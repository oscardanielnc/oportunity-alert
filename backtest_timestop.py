#!/usr/bin/env python3
"""
Time-Stop Grid Search Backtest
=======================================================
Encuentra los parametros optimos de time-stop (max_scans x adverse_pct)
para los 3 timeframes del watcher (1m, 5m, 15m).

Grid por TF:
  1m:  max_scans [5,10,15,20,30,45,60]  x  adverse [-1.0,0.0,0.2,0.3,0.5,0.7,1.0]
  5m:  max_scans [4,6,8,10,12,16,24]    x  adverse [-1.0,0.0,0.2,0.3,0.5,0.7,1.0]
  15m: max_scans [2,3,4,6,8,10,12]      x  adverse [-1.0,0.0,0.2,0.3,0.5,0.7,1.0]

adverse_pct = -1.0  => time-stop PURO: sale tras max_scans sin importar P&L
adverse_pct =  0.0  => sale si plano o perdiendo despues de max_scans
adverse_pct > 0     => sale solo si esta perdiendo >= X% despues de max_scans
BASELINE             => sin time-stop (max_scans=9999)

Metrica primaria: Profit Factor = gross_wins / abs(gross_losses)
Score compuesto: 0.5*PF + 0.3*WR + 0.2*avg_pnl (ponderado)
"""
import os, sys, time as _time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS

ALPACA_BASE = "https://data.alpaca.markets"
LIMA_OFFSET = -5
CONFIRM_REQ  = 2

TICKERS = ["NVDA", "AMD", "MU", "PLTR", "IONQ", "QBTS", "APP", "RDDT", "AVGO", "MSFT"]

GRID = {
    "1m": {
        "tf":   "1Min",  "htf": "5Min",   "weeks": 1,  "warmup": 30,
        "label": "1 minuto  | 1 semana historia",
        "max_scans_list": [5, 10, 15, 20, 30, 45, 60],
        "adverse_list":   [-1.0, 0.0, 0.2, 0.3, 0.5, 0.7, 1.0],
    },
    "5m": {
        "tf":   "5Min",  "htf": "15Min",  "weeks": 6,  "warmup": 30,
        "label": "5 minutos | 6 semanas historia",
        "max_scans_list": [4, 6, 8, 10, 12, 16, 24],
        "adverse_list":   [-1.0, 0.0, 0.2, 0.3, 0.5, 0.7, 1.0],
    },
    "15m": {
        "tf":   "15Min", "htf": "1Hour",  "weeks": 12, "warmup": 30,
        "label": "15 minutos | 12 semanas historia",
        "max_scans_list": [2, 3, 4, 6, 8, 10, 12],
        "adverse_list":   [-1.0, 0.0, 0.2, 0.3, 0.5, 0.7, 1.0],
    },
}


# -- API -------------------------------------------------------------------------

def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


def fetch_all(ticker, tf, start, limit=10000):
    bars, params = [], {
        "timeframe": tf, "start": start, "feed": "sip",
        "limit": 1000, "sort": "asc",
    }
    try:
        while True:
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=_hdrs(), timeout=30)
            r.raise_for_status()
            data  = r.json()
            chunk = data.get("bars") or []
            bars.extend(chunk)
            if not data.get("next_page_token") or len(bars) >= limit:
                break
            params["page_token"] = data["next_page_token"]
            _time.sleep(0.15)
    except Exception:
        pass
    return [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": int(b["v"])} for b in bars]


# -- Helpers ----------------------------------------------------------------------

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


# -- Simulacion ------------------------------------------------------------------

def simulate(bars, htf_bars, warmup, max_scans, adverse_pct):
    """
    Simulacion barra a barra con time-stop parametrizable.
      max_scans = 9999  => sin time-stop (baseline)
      adverse_pct = -1.0 => pure time-stop, sale siempre al cumplir max_scans
      adverse_pct >= 0   => sale solo si perdiendo >= adverse_pct%

    Optimizaciones O(n) vs O(n^2):
      - candles: ventana fija de 120 barras (EMA converge, S/R usa last-50 de todos modos)
      - htf: pointer que avanza en lugar de filter O(n) en cada barra
    """
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []
    htf_ptr    = 0          # pointer para HTF lookup O(1) amortizado

    for i in range(warmup, len(bars)):
        bar       = bars[i]
        cur_ts    = bar["t"]
        cur_price = bar["c"]

        # Ventana fija: EMA26 necesita 26+ barras, 120 es mas que suficiente
        candles = bars[max(0, i - 119):i + 1]

        # Avanzar pointer HTF hasta cur_ts (O(1) amortizado)
        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0, htf_ptr - 50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"

        # -- Time-stop ----------------------------------------------------------
        if position and max_scans < 9999:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep_pos = position["entry_price"]
            adverse = ((ep_pos - cur_price) / ep_pos * 100 if position["side"] == "LONG"
                       else (cur_price - ep_pos) / ep_pos * 100)
            if position["scans_held"] >= max_scans and adverse >= adverse_pct:
                no   = bars[i + 1]["o"] if i + 1 < len(bars) else cur_price
                nt   = lima_ts(bars[i + 1]) if i + 1 < len(bars) else lima_ts(bar)
                side = position["side"]
                pnl  = ((no - ep_pos) / ep_pos * 100 if side == "LONG"
                        else (ep_pos - no) / ep_pos * 100)
                trades.append({
                    "side": side, "in_t": position["entry_time"], "in_p": ep_pos,
                    "out_t": nt, "out_p": no, "pnl": round(pnl, 3), "note": "TIME-STOP",
                })
                position = None; lock_dir = None; lock_rem = 0
                cur_sig  = "ESPERAR"; cand_sig = None; cand_count = 0
                continue

        pos_state = "SIN_POSICION" if not position else position["side"]
        res       = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig   = res["signal"]

        if lock_rem > 0 and lock_dir:
            if (lock_dir == "SHORT" and raw_sig == "ENTRAR_SHORT") or \
               (lock_dir == "LONG"  and raw_sig == "ENTRAR_LONG"):
                raw_sig = "ESPERAR"
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0:
                lock_dir = None

        confirmed = False
        if raw_sig == cand_sig:
            cand_count += 1
            if cand_count >= CONFIRM_REQ and raw_sig != cur_sig:
                confirmed = True; cur_sig = raw_sig
            if cand_count >= CONFIRM_REQ:
                cand_sig = None; cand_count = 0
        else:
            cand_sig = raw_sig; cand_count = 1

        if not confirmed:
            continue

        if cur_sig == "ENTRAR_LONG":
            lock_dir = "SHORT"; lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig == "ENTRAR_SHORT":
            lock_dir = "LONG";  lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig in ("CERRAR_LONG", "CERRAR_SHORT", "ESPERAR"):
            lock_dir = None; lock_rem = 0

        ep = bars[i + 1]["o"] if i + 1 < len(bars) else bar["c"]
        et = lima_ts(bars[i + 1]) if i + 1 < len(bars) else lima_ts(bar)

        def close_pos(ep, et, note="", _pos=None):
            p = _pos or position
            if not p: return
            side = p["side"]
            pnl  = (ep - p["entry_price"]) / p["entry_price"] * 100
            if side == "SHORT": pnl = -pnl
            trades.append({
                "side": side, "in_t": p["entry_time"], "in_p": p["entry_price"],
                "out_t": et, "out_p": ep, "pnl": round(pnl, 3), "note": note,
            })

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"] == "SHORT":
                close_pos(ep, et, "inversion", position); position = None
            if not position:
                position = {"side": "LONG", "entry_price": ep, "entry_time": et, "scans_held": 0}
        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"] == "LONG":
                close_pos(ep, et, "inversion", position); position = None
            if not position:
                position = {"side": "SHORT", "entry_price": ep, "entry_time": et, "scans_held": 0}
        elif cur_sig == "CERRAR_LONG" and position and position["side"] == "LONG":
            close_pos(ep, et, "", position); position = None
        elif cur_sig == "CERRAR_SHORT" and position and position["side"] == "SHORT":
            close_pos(ep, et, "", position); position = None

    if position:
        lp   = bars[-1]["c"]
        lt   = lima_ts(bars[-1]) + "*"
        side = position["side"]
        pnl  = (lp - position["entry_price"]) / position["entry_price"] * 100
        if side == "SHORT": pnl = -pnl
        trades.append({
            "side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
            "out_t": lt, "out_p": lp, "pnl": round(pnl, 3), "note": "fin_periodo",
        })

    return trades


# -- Estadisticas -----------------------------------------------------------------

def stats(trades):
    if not trades:
        return {"n": 0, "wins": 0, "losses": 0, "pct_win": 0.0,
                "total": 0.0, "avg": 0.0, "pf": 0.0,
                "best": 0.0, "worst": 0.0, "tstops": 0, "tstop_pct": 0.0}
    pnls       = [t["pnl"] for t in trades]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]
    total      = sum(pnls)
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    tstops = sum(1 for t in trades if t.get("note") == "TIME-STOP")
    return {
        "n":         len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "pct_win":   round(len(wins) / len(trades) * 100),
        "total":     round(total, 2),
        "avg":       round(total / len(trades), 3),
        "pf":        pf,
        "best":      round(max(wins,   default=0.0), 2),
        "worst":     round(min(losses, default=0.0), 2),
        "tstops":    tstops,
        "tstop_pct": round(tstops / len(trades) * 100) if trades else 0,
    }


def composite_score(s):
    """Score compuesto: PF (50%) + WR (30%) + avg_pnl (20%). Penaliza <20 trades."""
    if s["n"] < 20:
        return -999.0
    pf_norm  = min(s["pf"], 6.0) / 6.0 * 5.0          # 0-5
    wr_norm  = (s["pct_win"] / 100.0) * 5.0             # 0-5
    avg_norm = min(max(s["avg"] * 15, 0.0), 5.0)        # 0-5
    return round(0.5 * pf_norm + 0.3 * wr_norm + 0.2 * avg_norm, 4)


def fmt(v, decimals=2, sign=True):
    s = f"{'+' if sign and v >= 0 else ''}{v:.{decimals}f}"
    return s


# -- Runner -----------------------------------------------------------------------

def run():
    W = 95
    now_utc = datetime.now(timezone.utc)
    start_utc_label = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*W}")
    print(f"  TIME-STOP GRID SEARCH BACKTEST  —  {start_utc_label} Lima")
    print(f"  Tickers ({len(TICKERS)}): {', '.join(TICKERS)}")
    print(f"  Grid: 7 max_scans x 7 adverse_pct = 49 combos + BASELINE por TF  (150 total)")
    print(f"  adverse=-1.0 = time-stop PURO (sale siempre al vencer scans)")
    print(f"{'='*W}")

    best_by_tf = {}

    for tf_key, cfg in GRID.items():
        print(f"\n{'-'*W}")
        print(f"  [{tf_key.upper()}]  {cfg['label']}")
        print(f"{'-'*W}")

        start_dt  = (now_utc - timedelta(weeks=cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start = (now_utc - timedelta(weeks=cfg["weeks"] + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # -- Fetch data ---------------------------------------------------------
        ticker_data = {}
        for ticker in TICKERS:
            sys.stdout.write(f"  Cargando {ticker}... ")
            sys.stdout.flush()
            bars     = fetch_all(ticker, cfg["tf"],  start_dt)
            htf_bars = fetch_all(ticker, cfg["htf"], htf_start)
            if len(bars) >= cfg["warmup"] + 5:
                ticker_data[ticker] = (bars, htf_bars)
                print(f"{len(bars)} barras OK")
            else:
                print(f"skip ({len(bars)} barras insuficientes)")

        if not ticker_data:
            print(f"  Sin datos para {tf_key} — skip")
            continue

        total_tickers = len(ticker_data)

        # -- Baseline (sin time-stop) -------------------------------------------
        base_trades = []
        for ticker, (bars, htf_bars) in ticker_data.items():
            base_trades += simulate(bars, htf_bars, cfg["warmup"], 9999, -999.0)
        base_s = stats(base_trades)
        print(f"\n  BASELINE (sin time-stop): {base_s['n']} trades | "
              f"WR {base_s['pct_win']}% | PnL {fmt(base_s['total'])}% | "
              f"Avg {fmt(base_s['avg'], 3)}% | PF {base_s['pf']:.2f}")

        # -- Grid search -------------------------------------------------------
        results = []
        n_combos = len(cfg["max_scans_list"]) * len(cfg["adverse_list"])
        done = 0
        print(f"  Corriendo {n_combos} combos x {total_tickers} tickers "
              f"= {n_combos * total_tickers} simulaciones...\n", end="")

        for ms in cfg["max_scans_list"]:
            for adv in cfg["adverse_list"]:
                all_trades = []
                for ticker, (bars, htf_bars) in ticker_data.items():
                    all_trades += simulate(bars, htf_bars, cfg["warmup"], ms, adv)
                s  = stats(all_trades)
                cs = composite_score(s)
                results.append({"max_scans": ms, "adverse": adv, "stats": s, "score": cs})
                done += 1
                sys.stdout.write(f"\r  [{done:>2}/{n_combos}] ms={ms:<3} adv={adv:>4}  "
                                 f"n={s['n']:>4}  WR={s['pct_win']:>3}%  "
                                 f"PF={s['pf']:.2f}  score={cs:.3f}   ")
                sys.stdout.flush()

        print(f"\r  Completado: {n_combos} combos evaluados.                              ")

        results.sort(key=lambda x: -x["score"])

        # -- Top 15 table ------------------------------------------------------
        print(f"\n  TOP 15 COMBINACIONES — [{tf_key.upper()}] (ordenado por score compuesto)")
        hdr = (f"  {'Rank':>4}  {'scans':>5}  {'adverse':>7}  "
               f"{'N':>5}  {'WR':>4}  {'PnL':>9}  {'Avg':>7}  "
               f"{'PF':>5}  {'TS':>6}  {'dPnL':>7}  {'dPF':>6}  {'Score':>6}")
        print(hdr)
        print(f"  {'-'*87}")
        for rank, r in enumerate(results[:15], 1):
            s       = r["stats"]
            adv_lbl = "PURO " if r["adverse"] < -0.5 else f"{r['adverse']:.1f}% "
            d_pnl   = s["total"] - base_s["total"]
            d_pf    = s["pf"]    - base_s["pf"]
            flag    = " <<" if rank == 1 else ("" if rank > 3 else "  <")
            ts_lbl  = f"{s['tstops']}({s['tstop_pct']}%)"
            print(f"  {rank:>4}  {r['max_scans']:>5}  {adv_lbl:>7}  "
                  f"{s['n']:>5}  {s['pct_win']:>3}%  {fmt(s['total']):>9}%  "
                  f"{fmt(s['avg'],3):>7}%  {s['pf']:>5.2f}  "
                  f"{ts_lbl:>6}  {fmt(d_pnl):>7}  {fmt(d_pf):>6}  "
                  f"{r['score']:>6.3f}{flag}")

        # -- Bottom 5 (worst) --------------------------------------------------
        worst5 = sorted(results, key=lambda x: x["score"])[:5]
        print(f"\n  PEORES 5 COMBINACIONES (para referencia):")
        for r in worst5:
            s       = r["stats"]
            adv_lbl = "PURO " if r["adverse"] < -0.5 else f"{r['adverse']:.1f}%"
            print(f"    scans={r['max_scans']:<3}  adv={adv_lbl:<5}  "
                  f"n={s['n']:<5}  WR={s['pct_win']}%  PF={s['pf']:.2f}  "
                  f"PnL={fmt(s['total'])}%  score={r['score']:.3f}")

        # -- Recommendation ---------------------------------------------------
        best   = results[0]
        bs     = best["stats"]
        d_pf   = bs["pf"]      - base_s["pf"]
        d_wr   = bs["pct_win"] - base_s["pct_win"]
        d_pnl  = bs["total"]   - base_s["total"]
        d_avg  = bs["avg"]     - base_s["avg"]
        adv_lbl = "PURO (sin condicion adversidad)" if best["adverse"] < -0.5 \
                  else f"{best['adverse']:.1f}%"

        # What's currently in super_backtest.py for this TF
        current = {"1m": (30, 0.5), "5m": (12, 0.5), "15m": (6, 0.5)}
        cur_ms, cur_adv = current.get(tf_key, (None, None))

        print(f"\n  >> RECOMENDACION [{tf_key.upper()}]:")
        print(f"     max_scans = {best['max_scans']}  |  adverse_pct = {adv_lbl}")
        print(f"     Trades: {bs['n']}  |  WR: {bs['pct_win']}%  |  "
              f"Avg: {fmt(bs['avg'],3)}%  |  PF: {bs['pf']:.2f}")
        print(f"     PnL total: {fmt(bs['total'])}%")
        print(f"     vs BASELINE:  dPnL={fmt(d_pnl)}pp  dPF={fmt(d_pf)}  "
              f"dWR={fmt(d_wr,0)}pp  dAvg={fmt(d_avg,3)}%")
        if cur_ms:
            cur_trades = []
            for ticker, (bars, htf_bars) in ticker_data.items():
                cur_trades += simulate(bars, htf_bars, cfg["warmup"], cur_ms, cur_adv)
            cur_s = stats(cur_trades)
            d2_pf  = bs["pf"]    - cur_s["pf"]
            d2_pnl = bs["total"] - cur_s["total"]
            d2_wr  = bs["pct_win"] - cur_s["pct_win"]
            print(f"     ACTUAL (ms={cur_ms},adv={cur_adv}):  "
                  f"PF={cur_s['pf']:.2f}  WR={cur_s['pct_win']}%  PnL={fmt(cur_s['total'])}%")
            print(f"     vs ACTUAL:  dPnL={fmt(d2_pnl)}pp  dPF={fmt(d2_pf)}  dWR={fmt(d2_wr,0)}pp")

        best_by_tf[tf_key] = {
            "max_scans": best["max_scans"],
            "adverse":   best["adverse"],
            "stats":     bs,
            "baseline":  base_s,
        }

    # -- Final summary ------------------------------------------------------------
    print(f"\n{'='*W}")
    print(f"  RESUMEN FINAL — MEJORES PARAMETROS TIME-STOP POR TIMEFRAME")
    print(f"  {'TF':<5}  {'scans':>5}  {'adverse':>7}  {'N':>5}  {'WR':>4}  "
          f"{'PnL':>9}  {'Avg':>7}  {'PF':>5}  {'dPF vs BASE':>11}  {'dWR':>6}")
    print(f"  {'-'*80}")
    for tf_key, d in best_by_tf.items():
        bs   = d["stats"]
        base = d["baseline"]
        adv_lbl = "PURO " if d["adverse"] < -0.5 else f"{d['adverse']:.1f}%"
        print(f"  {tf_key:<5}  {d['max_scans']:>5}  {adv_lbl:>7}  {bs['n']:>5}  "
              f"{bs['pct_win']:>3}%  {fmt(bs['total']):>9}%  {fmt(bs['avg'],3):>7}%  "
              f"{bs['pf']:>5.2f}  {fmt(bs['pf']-base['pf']):>11}  "
              f"{fmt(bs['pct_win']-base['pct_win'],0):>6}pp")
    print()
    print(f"  INTERPRETACION:")
    for tf_key, d in best_by_tf.items():
        bs   = d["stats"]
        base = d["baseline"]
        adv_lbl = "PURO " if d["adverse"] < -0.5 else f"{d['adverse']:.1f}%"
        mejora = bs["pf"] > base["pf"] and bs["total"] >= base["total"]
        estado = "IMPLEMENTAR" if mejora else "NEUTRAL/CUIDADO"
        print(f"  [{tf_key.upper()}] scans={d['max_scans']}, adv={adv_lbl.strip()} "
              f"=> {estado}  (PF {base['pf']:.2f} -> {bs['pf']:.2f}, "
              f"PnL {fmt(base['total'])}% -> {fmt(bs['total'])}%)")
    print()


if __name__ == "__main__":
    run()
