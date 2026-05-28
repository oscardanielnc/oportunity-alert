#!/usr/bin/env python3
"""
Super Backtest — 3 timeframes (1m, 5m, 15m) x 10 tickers x N semanas.
Compara rendimiento por timeframe, identifica el mejor para hoy.
Parametros time-stop ajustados: 30 scans (1m), 12 scans (5m), 6 scans (15m).
"""
import os, sys, time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS

ALPACA_BASE = "https://data.alpaca.markets"
LIMA_OFFSET = -5
CONFIRM_REQ = 2

# -- Tickers (10 representativos: large-cap, quantum, growth, sector defensivo) -
TICKERS = ["NVDA", "AMD", "MU", "PLTR", "IONQ", "QBTS", "APP", "RDDT", "AVGO", "MSFT"]

# -- Configuracion por timeframe ------------------------------------------------
TF_CONFIG = {
    "1m": {
        "tf":        "1Min",
        "htf":       "5Min",
        "weeks":     1,        # 1 semana de historia (muchas barras)
        "max_scans": 30,       # 30 min max por posicion
        "adverse":   0.5,      # 0.5% movimiento adverso para time-stop
        "warmup":    30,
        "label":     "1 minuto  (30-scan stop = 30 min)",
    },
    "5m": {
        "tf":        "5Min",
        "htf":       "15Min",
        "weeks":     6,        # 6 semanas de historia
        "max_scans": 12,       # 60 min max por posicion
        "adverse":   0.5,
        "warmup":    30,
        "label":     "5 minutos (12-scan stop = 60 min)",
    },
    "15m": {
        "tf":        "15Min",
        "htf":       "1Hour",
        "weeks":     12,       # 12 semanas de historia
        "max_scans": 6,        # 90 min max por posicion
        "adverse":   0.5,
        "warmup":    30,
        "label":     "15 minutos (6-scan stop  = 90 min)",
    },
}

TODAY_DATE  = "2026-05-27"
TODAY_START = "2026-05-27T08:00:00Z"  # 3am Lima
TODAY_END   = "2026-05-27T23:59:00Z"


# -- API helpers ----------------------------------------------------------------

def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


def fetch_all(ticker, tf, start, end=None, limit=10000):
    bars, params = [], {
        "timeframe": tf, "start": start, "feed": "sip",
        "limit": 1000, "sort": "asc",
    }
    if end:
        params["end"] = end
    try:
        while True:
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=_hdrs(), timeout=30)
            r.raise_for_status()
            data   = r.json()
            chunk  = data.get("bars") or []
            bars.extend(chunk)
            if not data.get("next_page_token") or len(bars) >= limit:
                break
            params["page_token"] = data["next_page_token"]
            time.sleep(0.2)
    except Exception as e:
        pass
    return [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": int(b["v"])} for b in bars]


# -- Lima time helpers ----------------------------------------------------------

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


def lima_date(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d")


def is_today(bar):
    return lima_date(bar) == TODAY_DATE


# -- Simulacion v3 (con todas las mejoras) -------------------------------------

def simulate(bars, htf_bars, max_scans, adverse_pct, warmup=30):
    """
    Simulacion barra a barra con:
      - Confirmacion 2 scans
      - Direction lock DIRECTION_LOCK_SCANS scans
      - HTF trend filter
      - Time-stop: max_scans con adverse_pct% en contra
    Retorna lista de trades con campo 'date' para filtrar por dia.
    """
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []

    for i in range(warmup, len(bars)):
        candles   = bars[:i + 1]
        bar       = candles[-1]
        cur_ts    = bar["t"]
        cur_price = bar["c"]

        htf_avail = [b for b in htf_bars if b["t"] < cur_ts]
        htf_trend = compute_htf_trend(htf_avail) if len(htf_avail) >= 20 else "NEUTRAL"

        # Time-stop
        if position:
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
                    "ticker": position.get("ticker", "?"),
                    "side": side, "in_t": position["entry_time"], "in_p": ep_pos,
                    "out_t": nt, "out_p": no, "pnl": round(pnl, 3),
                    "note": "TIME-STOP", "date": lima_date(bar),
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
        ed = lima_date(bars[i + 1]) if i + 1 < len(bars) else lima_date(bar)

        def close_pos(ep, et, ed, note=""):
            if not position: return
            side = position["side"]
            pnl  = (ep - position["entry_price"]) / position["entry_price"] * 100
            if side == "SHORT": pnl = -pnl
            trades.append({
                "ticker": position.get("ticker", "?"),
                "side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
                "out_t": et, "out_p": ep, "pnl": round(pnl, 3), "note": note,
                "date": position.get("date", ed),
            })

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"] == "SHORT":
                close_pos(ep, et, ed, "inversion"); position = None
            if not position:
                position = {"ticker": bar.get("ticker", "?"), "side": "LONG",
                            "entry_price": ep, "entry_time": et, "scans_held": 0, "date": ed}
        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"] == "LONG":
                close_pos(ep, et, ed, "inversion"); position = None
            if not position:
                position = {"ticker": bar.get("ticker", "?"), "side": "SHORT",
                            "entry_price": ep, "entry_time": et, "scans_held": 0, "date": ed}
        elif cur_sig == "CERRAR_LONG" and position and position["side"] == "LONG":
            close_pos(ep, et, ed); position = None
        elif cur_sig == "CERRAR_SHORT" and position and position["side"] == "SHORT":
            close_pos(ep, et, ed); position = None

    if position:
        lp   = bars[-1]["c"]
        lt   = lima_ts(bars[-1]) + "*"
        side = position["side"]
        pnl  = (lp - position["entry_price"]) / position["entry_price"] * 100
        if side == "SHORT": pnl = -pnl
        trades.append({
            "ticker": position.get("ticker", "?"),
            "side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
            "out_t": lt, "out_p": lp, "pnl": round(pnl, 3), "note": "fin_periodo",
            "date": position.get("date", "?"),
        })

    return trades


# -- Estadisticas ---------------------------------------------------------------

def stats(trades):
    if not trades:
        return {"n": 0, "wins": 0, "losses": 0, "pct_win": 0,
                "total": 0, "avg": 0, "best": 0, "worst": 0, "tstops": 0}
    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = sum(pnls)
    return {
        "n":      len(trades),
        "wins":   len(wins),
        "losses": len(losses),
        "pct_win": round(len(wins) / len(trades) * 100),
        "total":  round(total, 2),
        "avg":    round(total / len(trades), 3),
        "best":   round(max(wins,   default=0), 2),
        "worst":  round(min(losses, default=0), 2),
        "tstops": sum(1 for t in trades if t.get("note") == "TIME-STOP"),
    }


def fmt_pnl(v):
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


# -- Runner ---------------------------------------------------------------------

def run():
    W = 82
    now_utc = datetime.now(timezone.utc)
    start_utc = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*W}")
    print(f"  SUPER BACKTEST — {start_utc} Lima")
    print(f"  Tickers ({len(TICKERS)}): {', '.join(TICKERS)}")
    print(f"  Timeframes: 1m (1 semana)  |  5m (6 semanas)  |  15m (12 semanas)")
    print(f"  Time-stop ajustado: 30/12/6 scans con 0.5% adverso")
    print(f"{'='*W}\n")

    results   = {}   # tf -> {ticker -> {all_trades, today_trades}}
    tf_totals = {}   # tf -> aggregated stats

    for tf_key, cfg in TF_CONFIG.items():
        print(f"[{tf_key}] Iniciando — {cfg['label']}")
        results[tf_key]   = {}
        all_tf_trades     = []
        today_tf_trades   = []

        start_dt = (now_utc - timedelta(weeks=cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start = (now_utc - timedelta(weeks=cfg["weeks"] + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for ticker in TICKERS:
            sys.stdout.write(f"  {ticker}... ")
            sys.stdout.flush()

            bars     = fetch_all(ticker, cfg["tf"],  start_dt, limit=8000)
            htf_bars = fetch_all(ticker, cfg["htf"], htf_start, limit=3000)

            # Agregar ticker a cada barra para trazabilidad
            for b in bars:     b["ticker"] = ticker
            for b in htf_bars: b["ticker"] = ticker

            if len(bars) < cfg["warmup"] + 5:
                print(f"skip ({len(bars)} barras)")
                continue

            trades = simulate(bars, htf_bars, cfg["max_scans"], cfg["adverse"], cfg["warmup"])

            today_trades = [t for t in trades if t.get("date") == TODAY_DATE]

            results[tf_key][ticker] = {
                "all":   trades,
                "today": today_trades,
                "bars":  len(bars),
            }
            all_tf_trades   += trades
            today_tf_trades += today_trades

            s = stats(trades)
            print(f"{len(bars)} barras | {s['n']} trades | WR {s['pct_win']}% | {fmt_pnl(s['total'])}")

        tf_totals[tf_key] = {
            "all":   stats(all_tf_trades),
            "today": stats(today_tf_trades),
            "raw_all":   all_tf_trades,
            "raw_today": today_tf_trades,
        }
        print()

    # -- Tabla resumen por ticker en cada timeframe -----------------------------
    for tf_key, cfg in TF_CONFIG.items():
        print(f"\n{'-'*W}")
        print(f"  [{tf_key.upper()}] {cfg['label']}  —  Resultados por ticker")
        print(f"  {'Ticker':<6}  {'Barras':>6}  {'Trades':>6}  {'WR':>5}  {'PnL Total':>10}  "
              f"{'Avg/trade':>9}  {'Mejor':>7}  {'Peor':>8}  {'T-Stops':>7}")
        print(f"  {'-'*72}")
        for ticker in TICKERS:
            if ticker not in results[tf_key]:
                print(f"  {ticker:<6}  -- sin datos")
                continue
            d = results[tf_key][ticker]
            s = stats(d["all"])
            if s["n"] == 0:
                print(f"  {ticker:<6}  {d['bars']:>6}  {'0':>6}  {'-':>5}  {'-':>10}  {'-':>9}  {'-':>7}  {'-':>8}  {'-':>7}")
                continue
            print(f"  {ticker:<6}  {d['bars']:>6}  {s['n']:>6}  {s['pct_win']:>4}%  "
                  f"{fmt_pnl(s['total']):>10}  {'+' if s['avg']>=0 else ''}{s['avg']:.3f}%  "
                  f"{s['best']:>+6.2f}%  {s['worst']:>+7.2f}%  {s['tstops']:>7}")

        s_all   = tf_totals[tf_key]["all"]
        s_today = tf_totals[tf_key]["today"]
        print(f"  {'-'*72}")
        print(f"  {'TOTAL':<6}  {'-':>6}  {s_all['n']:>6}  {s_all['pct_win']:>4}%  "
              f"{fmt_pnl(s_all['total']):>10}  {'+' if s_all['avg']>=0 else ''}{s_all['avg']:.3f}%  "
              f"{s_all['best']:>+6.2f}%  {s_all['worst']:>+7.2f}%  {s_all['tstops']:>7}")
        print(f"  HOY ({TODAY_DATE}): {s_today['n']} trades | WR {s_today['pct_win']}% | "
              f"PnL {fmt_pnl(s_today['total'])} | Avg {'+' if s_today['avg']>=0 else ''}{s_today['avg']:.3f}%")

    # -- Comparativa final entre timeframes -------------------------------------
    print(f"\n{'='*W}")
    print(f"  COMPARATIVA GLOBAL — v3 (time-stop ajustado)")
    print(f"  {'TF':<5}  {'Periodo':>8}  {'Trades':>6}  {'WR':>5}  {'PnL Total':>10}  "
          f"{'Avg/trade':>9}  {'Mejor':>7}  {'Peor':>8}  {'T-Stops':>7}")
    print(f"  {'-'*72}")
    for tf_key, cfg in TF_CONFIG.items():
        s = tf_totals[tf_key]["all"]
        weeks_label = f"{cfg['weeks']}sem"
        if s["n"] == 0:
            print(f"  {tf_key:<5}  {weeks_label:>8}  {'0':>6}  {'-':>5}")
            continue
        print(f"  {tf_key:<5}  {weeks_label:>8}  {s['n']:>6}  {s['pct_win']:>4}%  "
              f"{fmt_pnl(s['total']):>10}  {'+' if s['avg']>=0 else ''}{s['avg']:.3f}%  "
              f"{s['best']:>+6.2f}%  {s['worst']:>+7.2f}%  {s['tstops']:>7}")

    print(f"\n  RENDIMIENTO HOY ({TODAY_DATE}) por timeframe:")
    print(f"  {'TF':<5}  {'Trades':>6}  {'WR':>5}  {'PnL Hoy':>10}  {'Avg/trade':>9}  {'Mejor Hoy':>10}  {'Peor Hoy':>9}")
    print(f"  {'-'*65}")
    best_today_tf  = None
    best_today_pnl = -9999
    for tf_key in TF_CONFIG:
        s = tf_totals[tf_key]["today"]
        if s["n"] == 0:
            print(f"  {tf_key:<5}  {'0':>6}  {'-':>5}  {'-':>10}")
            continue
        print(f"  {tf_key:<5}  {s['n']:>6}  {s['pct_win']:>4}%  "
              f"{fmt_pnl(s['total']):>10}  {'+' if s['avg']>=0 else ''}{s['avg']:.3f}%  "
              f"{s['best']:>+9.2f}%  {s['worst']:>+8.2f}%")
        if s["total"] > best_today_pnl:
            best_today_pnl = s["total"]
            best_today_tf  = tf_key

    if best_today_tf:
        print(f"\n  >> MEJOR TIMEFRAME HOY: {best_today_tf.upper()} "
              f"({fmt_pnl(best_today_pnl)} acumulado en {TODAY_DATE})")

    # -- Detalle trades de hoy en el mejor timeframe ----------------------------
    if best_today_tf:
        raw = tf_totals[best_today_tf]["raw_today"]
        if raw:
            print(f"\n  TRADES HOY — {best_today_tf.upper()} (detalle):")
            print(f"  {'#':<3}  {'Tick':<5}  {'Side':<5}  {'In':>5}  {'Out':>5}  "
                  f"{'In$':>8}  {'Out$':>8}  {'PnL':>8}  Nota")
            print(f"  {'-'*72}")
            for n, t in enumerate(sorted(raw, key=lambda x: x["in_t"]), 1):
                icon = "WIN" if t["pnl"] > 0 else ("BE " if t["pnl"] == 0 else "LOS")
                print(f"  {n:<3}  {t['ticker']:<5}  {t['side']:<5}  {t['in_t']:>5}  "
                      f"{t['out_t']:>5}  ${t['in_p']:>7.2f}  ${t['out_p']:>7.2f}  "
                      f"{t['pnl']:>+7.3f}%  {icon} {t.get('note','')}")

    # -- Top 5 peores trades globales (riesgo real) ----------------------------
    print(f"\n  PEORES TRADES GLOBALES (todos los TF combinados):")
    all_combined = []
    for tf_key in TF_CONFIG:
        for t in tf_totals[tf_key]["raw_all"]:
            t2 = dict(t); t2["tf"] = tf_key
            all_combined.append(t2)
    worst5 = sorted(all_combined, key=lambda t: t["pnl"])[:8]
    for t in worst5:
        print(f"    [{t['tf']}] {t['ticker']:<5}  {t['side']:<5}  "
              f"{t['in_t']}->{t['out_t']}  ${t['in_p']:.2f}->${t['out_p']:.2f}  "
              f"{t['pnl']:+.3f}%  [{t.get('note','')}]")
    print()


if __name__ == "__main__":
    run()
