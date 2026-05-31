import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
Ticker Segmentation Backtest
=============================================================
Segmenta tickers por categoria y encuentra los parametros optimos
para cada categoria x timeframe mediante grid search exhaustivo.

CATEGORIAS (basadas en comportamiento de precio y volatilidad):
  LARGE_CAP : NVDA, AMD, AVGO, MU, MSFT  — liquidos, ATR moderado
  QUANTUM   : IONQ, QBTS                  — volatilidad extrema
  AI_SW     : PLTR, APP, RDDT             — AI/software/social

PARAMETROS GRID (324 combos por categoria x TF = 2916 total):
  threshold_adj : [0, +1, +2]     ajuste al umbral base de entrada
  n_binary_min  : [1, 2, 3]       minimo de criterios binarios activos
  pattern_cap   : [2, 4]          cap al puntaje de patrones (4=sin cap)
  confirm_req   : [2, 3]          scans consecutivos para confirmar entrada
  max_scans     : 3 valores x TF  duracion maxima de posicion (time-stop)
  adverse_pct   : 3 valores x TF  adversidad minima para activar time-stop

Tambien: analisis per-ticker dentro de cada categoria y LONG vs SHORT.
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

# ── Categorias ────────────────────────────────────────────────────────────────
SEGMENTS = {
    "LARGE_CAP": {
        "tickers": ["NVDA", "AMD", "AVGO", "MU", "MSFT"],
        "desc":    "Large-cap semi/tech — alta liquidez, ATR moderado",
    },
    "QUANTUM": {
        "tickers": ["IONQ", "QBTS"],
        "desc":    "Quantum computing — volatilidad extrema, ATR alto",
    },
    "AI_SW": {
        "tickers": ["PLTR", "APP", "RDDT"],
        "desc":    "AI software / social — volatilidad media-alta",
    },
}

# ── TF config (base params, grid los variara) ─────────────────────────────────
TF_BASE = {
    "1m":  {"tf":"1Min",  "htf":"5Min",  "weeks":1,  "warmup":30,
            "max_scans_grid":[15,20,25],  "adverse_grid":[0.3,0.5,0.7]},
    "5m":  {"tf":"5Min",  "htf":"15Min", "weeks":6,  "warmup":30,
            "max_scans_grid":[8,12,16],   "adverse_grid":[0.0,0.3,0.5]},
    "15m": {"tf":"15Min", "htf":"1Hour", "weeks":12, "warmup":30,
            "max_scans_grid":[3,4,6],     "adverse_grid":[0.5,1.0,1.5]},
}

# ── Grid de parametros de senal ───────────────────────────────────────────────
GRID = {
    "threshold_adj": [0, 1, 2],      # ajuste al umbral base (5 neutral / 4 bullish)
    "n_binary_min":  [1, 2, 3],      # minimo de L1-L6 activos para entrar
    "pattern_cap":   [2, 4],         # 2=actual  4=sin cap (mismo que original)
    "confirm_req":   [2, 3],         # confirmaciones necesarias
}


# ── API ───────────────────────────────────────────────────────────────────────

def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY",""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY",""),
    }

def fetch_all(ticker, tf, start, limit=10000):
    bars, params = [], {"timeframe":tf,"start":start,"feed":"sip","limit":1000,"sort":"asc"}
    try:
        while True:
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=_hdrs(), timeout=30)
            r.raise_for_status()
            data = r.json()
            bars.extend(data.get("bars") or [])
            if not data.get("next_page_token") or len(bars) >= limit: break
            params["page_token"] = data["next_page_token"]
            _time.sleep(0.12)
    except Exception: pass
    return [{"t":b["t"],"o":float(b["o"]),"h":float(b["h"]),
             "l":float(b["l"]),"c":float(b["c"]),"v":int(b["v"])} for b in bars]

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


# ── Custom entry decision con parametros inyectados ──────────────────────────

def custom_entry(result, pos_state, threshold_adj, n_binary_min, pattern_cap):
    """
    Recalcula la decision de entrada usando criterios crudos del resultado
    de compute_signal con parametros personalizados.
    Las salidas (CERRAR, PRECAUCION, MANTENER) pasan sin cambio.
    """
    if pos_state != "SIN_POSICION":
        return result["signal"]

    crit = result.get("criteria", {})
    htf  = result.get("htf_trend", "NEUTRAL")

    nl = crit.get("n_binary_long",  sum([crit.get("L1",False), crit.get("L2",False),
                                         crit.get("L3",False), crit.get("L4",False),
                                         crit.get("L6",False)]))
    ns = crit.get("n_binary_short", sum([crit.get("S1",False), crit.get("S2",False),
                                         crit.get("S3",False), crit.get("S4",False),
                                         crit.get("S6",False)]))

    # Puntaje con pattern_cap personalizado (usa bull_pscore RAW antes del cap interno)
    bull_raw = crit.get("bull_pscore", 0)
    bear_raw = crit.get("bear_pscore", 0)
    score_long  = nl + min(bull_raw, pattern_cap)
    score_short = ns + min(bear_raw, pattern_cap)

    # Umbrales base + ajuste
    if htf == "BULLISH":
        lt, st = max(3, 4 + threshold_adj), min(9, 8 + threshold_adj)
    elif htf == "BEARISH":
        lt, st = min(9, 8 + threshold_adj), max(3, 4 + threshold_adj)
    else:
        lt, st = max(3, 5 + threshold_adj), max(3, 5 + threshold_adj)

    can_l = score_long  >= lt and nl >= n_binary_min
    can_s = score_short >= st and ns >= n_binary_min

    if   can_l: return "ENTRAR_LONG"
    elif can_s: return "ENTRAR_SHORT"
    return "ESPERAR"


# ── Simulacion con parametros inyectados ──────────────────────────────────────

def simulate(bars, htf_bars, warmup, max_scans, adverse_pct,
             threshold_adj, n_binary_min, pattern_cap, confirm_req,
             ticker="?"):
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []
    htf_ptr    = 0

    for i in range(warmup, len(bars)):
        bar       = bars[i]
        cur_ts    = bar["t"]
        cur_price = bar["c"]
        candles   = bars[max(0, i-119):i+1]

        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0, htf_ptr-50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"

        pos_state = "SIN_POSICION" if not position else position["side"]

        # Time-stop
        if position and max_scans < 9999:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep = position["entry_price"]
            adverse = ((ep - cur_price)/ep*100 if pos_state=="LONG"
                       else (cur_price - ep)/ep*100)
            if position["scans_held"] >= max_scans and adverse >= adverse_pct:
                no   = bars[i+1]["o"] if i+1 < len(bars) else cur_price
                nt   = lima_ts(bars[i+1]) if i+1 < len(bars) else lima_ts(bar)
                pnl  = ((no-ep)/ep*100 if pos_state=="LONG" else (ep-no)/ep*100)
                trades.append({"ticker":ticker,"side":pos_state,
                               "in_t":position["entry_time"],"in_p":ep,
                               "out_t":nt,"out_p":no,"pnl":round(pnl,3),"note":"TS"})
                position=None; lock_dir=None; lock_rem=0
                cur_sig="ESPERAR"; cand_sig=None; cand_count=0
                continue

        result  = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig = custom_entry(result, pos_state, threshold_adj, n_binary_min, pattern_cap)

        if lock_rem > 0 and lock_dir:
            if (lock_dir=="SHORT" and raw_sig=="ENTRAR_SHORT") or \
               (lock_dir=="LONG"  and raw_sig=="ENTRAR_LONG"):
                raw_sig = "ESPERAR"
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0: lock_dir = None

        confirmed = False
        if raw_sig == cand_sig:
            cand_count += 1
            if cand_count >= confirm_req and raw_sig != cur_sig:
                confirmed = True; cur_sig = raw_sig
            if cand_count >= confirm_req:
                cand_sig = None; cand_count = 0
        else:
            cand_sig = raw_sig; cand_count = 1

        if not confirmed: continue

        if cur_sig == "ENTRAR_LONG":   lock_dir="SHORT"; lock_rem=DIRECTION_LOCK_SCANS
        elif cur_sig == "ENTRAR_SHORT": lock_dir="LONG";  lock_rem=DIRECTION_LOCK_SCANS
        elif cur_sig in ("CERRAR_LONG","CERRAR_SHORT","ESPERAR"):
            lock_dir=None; lock_rem=0

        ep2 = bars[i+1]["o"] if i+1 < len(bars) else bar["c"]
        et2 = lima_ts(bars[i+1]) if i+1 < len(bars) else lima_ts(bar)

        def close_pos(ep2, et2, note="", _p=None):
            p = _p or position
            if not p: return
            side = p["side"]
            pnl  = (ep2 - p["entry_price"])/p["entry_price"]*100
            if side == "SHORT": pnl = -pnl
            trades.append({"ticker":ticker,"side":side,
                           "in_t":p["entry_time"],"in_p":p["entry_price"],
                           "out_t":et2,"out_p":ep2,"pnl":round(pnl,3),"note":note})

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"]=="SHORT":
                close_pos(ep2,et2,"inv",position); position=None
            if not position:
                position={"side":"LONG","entry_price":ep2,"entry_time":et2,"scans_held":0}
        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"]=="LONG":
                close_pos(ep2,et2,"inv",position); position=None
            if not position:
                position={"side":"SHORT","entry_price":ep2,"entry_time":et2,"scans_held":0}
        elif cur_sig=="CERRAR_LONG"  and position and position["side"]=="LONG":
            close_pos(ep2,et2,"",position); position=None
        elif cur_sig=="CERRAR_SHORT" and position and position["side"]=="SHORT":
            close_pos(ep2,et2,"",position); position=None

    if position:
        lp=bars[-1]["c"]; lt2=lima_ts(bars[-1])+"*"; side=position["side"]
        pnl=(lp-position["entry_price"])/position["entry_price"]*100
        if side=="SHORT": pnl=-pnl
        trades.append({"ticker":ticker,"side":side,
                       "in_t":position["entry_time"],"in_p":position["entry_price"],
                       "out_t":lt2,"out_p":lp,"pnl":round(pnl,3),"note":"fp"})
    return trades


# ── Estadisticas ──────────────────────────────────────────────────────────────

def stats(trades):
    if not trades:
        return {"n":0,"wins":0,"losses":0,"pct_win":0.0,"total":0.0,
                "avg":0.0,"pf":0.0,"best":0.0,"worst":0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    total=sum(pnls)
    gw=sum(wins); gl=abs(sum(losses))
    pf=round(gw/gl,2) if gl>0 else (99.0 if gw>0 else 0.0)
    return {"n":len(trades),"wins":len(wins),"losses":len(losses),
            "pct_win":round(len(wins)/len(trades)*100),
            "total":round(total,2),"avg":round(total/len(trades),3),
            "pf":pf,"best":round(max(wins,default=0.0),2),
            "worst":round(min(losses,default=0.0),2)}

def composite_score(s, min_trades=8):
    if s["n"] < min_trades: return -999.0
    pf_n  = min(s["pf"], 5.0)/5.0*5
    wr_n  = (s["pct_win"]/100)*5
    avg_n = min(max(s["avg"]*15, 0), 5)
    return round(0.5*pf_n + 0.3*wr_n + 0.2*avg_n, 4)

def fmt(v, d=2):
    return f"{'+' if v>=0 else ''}{v:.{d}f}"


# ── Runner ─────────────────────────────────────────────────────────────────────

def run():
    W = 96
    now_utc = datetime.now(timezone.utc)
    label   = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*W}")
    print(f"  TICKER SEGMENTATION BACKTEST  --  {label} Lima")
    print(f"  Segmentos: {', '.join(SEGMENTS.keys())} | "
          f"TFs: 1m(1sem) 5m(6sem) 15m(12sem)")
    print(f"  Grid: {3}x{3}x{2}x{2}x{3}x{3} = 324 combos/grupo  |  "
          f"Total: {324*len(SEGMENTS)*3} simulaciones")
    print(f"{'='*W}")

    # Acumular recomendaciones para tabla final
    final_recs = {}  # (seg, tf) -> best_combo

    for seg_name, seg_cfg in SEGMENTS.items():
        tickers = seg_cfg["tickers"]
        print(f"\n{'#'*W}")
        print(f"  SEGMENTO: {seg_name}  —  {seg_cfg['desc']}")
        print(f"  Tickers: {', '.join(tickers)}")
        print(f"{'#'*W}")

        for tf_key, tf_cfg in TF_BASE.items():
            print(f"\n  [{tf_key.upper()}] Cargando datos...")
            start_dt  = (now_utc - timedelta(weeks=tf_cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
            htf_start = (now_utc - timedelta(weeks=tf_cfg["weeks"]+2)).strftime("%Y-%m-%dT%H:%M:%SZ")

            ticker_data = {}
            for ticker in tickers:
                sys.stdout.write(f"    {ticker}... "); sys.stdout.flush()
                bars     = fetch_all(ticker, tf_cfg["tf"],  start_dt)
                htf_bars = fetch_all(ticker, tf_cfg["htf"], htf_start)
                if len(bars) >= tf_cfg["warmup"]+5:
                    ticker_data[ticker] = (bars, htf_bars)
                    print(f"{len(bars)} barras")
                else:
                    print(f"skip")

            if not ticker_data:
                print(f"  Sin datos para [{tf_key}] {seg_name}")
                continue

            # ── Baseline: parametros actuales del sistema ──────────────────────
            # threshold_adj=0, n_binary_min=2, pattern_cap=2, confirm_req=2
            # max_scans y adverse = valores optimizados
            base_ms  = tf_cfg["max_scans_grid"][1]  # valor medio del grid
            base_adv = tf_cfg["adverse_grid"][1]
            base_trades = []
            for ticker, (bars, htf) in ticker_data.items():
                base_trades += simulate(bars, htf, tf_cfg["warmup"],
                                        base_ms, base_adv, 0, 2, 2, 2, ticker)
            base_s = stats(base_trades)
            print(f"\n  BASELINE [{tf_key}] (th+0 n2 pat2 conf2 ms={base_ms} adv={base_adv}): "
                  f"n={base_s['n']} WR={base_s['pct_win']}% "
                  f"PnL={fmt(base_s['total'])}% Avg={fmt(base_s['avg'],3)}% PF={base_s['pf']:.2f}")

            # ── Grid search ────────────────────────────────────────────────────
            total_combos = (len(GRID["threshold_adj"]) * len(GRID["n_binary_min"]) *
                            len(GRID["pattern_cap"])    * len(GRID["confirm_req"]) *
                            len(tf_cfg["max_scans_grid"]) * len(tf_cfg["adverse_grid"]))
            print(f"  Corriendo {total_combos} combos x {len(ticker_data)} tickers...", end="")

            results = []
            done = 0
            for th in GRID["threshold_adj"]:
                for nb in GRID["n_binary_min"]:
                    for pc in GRID["pattern_cap"]:
                        for cr in GRID["confirm_req"]:
                            for ms in tf_cfg["max_scans_grid"]:
                                for adv in tf_cfg["adverse_grid"]:
                                    all_trades = []
                                    for ticker, (bars, htf) in ticker_data.items():
                                        all_trades += simulate(
                                            bars, htf, tf_cfg["warmup"],
                                            ms, adv, th, nb, pc, cr, ticker)
                                    s  = stats(all_trades)
                                    cs = composite_score(s)
                                    results.append({
                                        "th":th,"nb":nb,"pc":pc,"cr":cr,"ms":ms,"adv":adv,
                                        "stats":s,"score":cs,
                                    })
                                    done += 1
            print(f"\r  Completado: {done} combos evaluados.                           ")

            results.sort(key=lambda x: -x["score"])

            # ── Top 15 ────────────────────────────────────────────────────────
            print(f"\n  TOP 15 [{seg_name} | {tf_key.upper()}] — ordenado por score compuesto")
            print(f"  {'R':>3}  {'th':>3}  {'nb':>3}  {'pc':>3}  {'cr':>3}  "
                  f"{'ms':>4}  {'adv':>5}  {'N':>5}  {'WR':>4}  "
                  f"{'PnL':>8}  {'Avg':>7}  {'PF':>5}  {'Score':>6}")
            print(f"  {'-'*80}")
            for rank, r in enumerate(results[:15], 1):
                s = r["stats"]
                if s["n"] == 0: continue
                flag = " <<" if rank==1 else (" <" if rank<=3 else "")
                d_pf = s["pf"] - base_s["pf"]
                print(f"  {rank:>3}  {r['th']:>+3}  {r['nb']:>3}  {r['pc']:>3}  {r['cr']:>3}  "
                      f"{r['ms']:>4}  {r['adv']:>5.1f}  {s['n']:>5}  {s['pct_win']:>3}%  "
                      f"{fmt(s['total']):>8}%  {fmt(s['avg'],3):>7}%  "
                      f"{s['pf']:>5.2f}  {r['score']:>6.3f}{flag}")

            # ── Peores 5 (referencia) ─────────────────────────────────────────
            worst5 = sorted([r for r in results if r["stats"]["n"]>0],
                            key=lambda x: x["score"])[:5]
            print(f"\n  PEORES 5 (referencia):")
            for r in worst5:
                s = r["stats"]
                print(f"    th={r['th']:+} nb={r['nb']} pc={r['pc']} cr={r['cr']} "
                      f"ms={r['ms']} adv={r['adv']:.1f}  "
                      f"n={s['n']} WR={s['pct_win']}% PF={s['pf']:.2f} score={r['score']:.3f}")

            # ── Recommendation ────────────────────────────────────────────────
            best  = results[0]
            bs    = best["stats"]
            d_pf  = bs["pf"]      - base_s["pf"]
            d_wr  = bs["pct_win"] - base_s["pct_win"]
            d_pnl = bs["total"]   - base_s["total"]
            d_avg = bs["avg"]     - base_s["avg"]

            print(f"\n  >> RECOMENDACION [{seg_name} | {tf_key.upper()}]:")
            print(f"     threshold_adj={best['th']:+}  n_binary_min={best['nb']}  "
                  f"pattern_cap={best['pc']}  confirm_req={best['cr']}")
            print(f"     max_scans={best['ms']}  adverse_pct={best['adv']:.1f}%")
            print(f"     N={bs['n']}  WR={bs['pct_win']}%  Avg={fmt(bs['avg'],3)}%  PF={bs['pf']:.2f}")
            print(f"     vs BASELINE: dPF={fmt(d_pf)}  dWR={fmt(d_wr,0)}pp  "
                  f"dPnL={fmt(d_pnl)}pp  dAvg={fmt(d_avg,3)}%")

            final_recs[(seg_name, tf_key)] = {
                "best": best, "baseline": base_s,
                "tickers": list(ticker_data.keys()),
            }

            # ── Per-ticker en best config ─────────────────────────────────────
            print(f"\n  PER-TICKER en config optima [{seg_name} | {tf_key.upper()}]:")
            print(f"  {'Ticker':>7}  {'N':>5}  {'WR':>4}  {'PnL':>8}  {'Avg':>7}  "
                  f"{'PF':>5}  {'L-WR':>6}  {'S-WR':>6}  vs BASE")
            print(f"  {'-'*72}")
            ticker_results = []
            for ticker, (bars, htf) in ticker_data.items():
                t_trades = simulate(bars, htf, tf_cfg["warmup"],
                                    best["ms"], best["adv"],
                                    best["th"], best["nb"], best["pc"], best["cr"], ticker)
                ts = stats(t_trades)
                long_t  = [t for t in t_trades if t["side"]=="LONG"]
                short_t = [t for t in t_trades if t["side"]=="SHORT"]
                wr_l = round(sum(1 for t in long_t  if t["pnl"]>0)/len(long_t) *100) if long_t  else 0
                wr_s = round(sum(1 for t in short_t if t["pnl"]>0)/len(short_t)*100) if short_t else 0
                ticker_results.append((ticker, ts, wr_l, len(long_t), wr_s, len(short_t)))

            # Sort by PF descending
            ticker_results.sort(key=lambda x: -x[1]["pf"])
            for ticker, ts, wr_l, nl, wr_s, ns in ticker_results:
                if ts["n"] == 0:
                    print(f"  {ticker:>7}  {'0':>5}  --  sin trades con esta config")
                    continue
                # Baseline for this ticker
                bt = simulate(ticker_data[ticker][0], ticker_data[ticker][1],
                              tf_cfg["warmup"], base_ms, base_adv, 0, 2, 2, 2, ticker)
                bts = stats(bt)
                d_pf_t = ts["pf"] - bts["pf"]
                flag_t = " ++ MEJOR" if d_pf_t>0.2 else (" -- PEOR" if d_pf_t<-0.2 else "")
                print(f"  {ticker:>7}  {ts['n']:>5}  {ts['pct_win']:>3}%  "
                      f"{fmt(ts['total']):>8}%  {fmt(ts['avg'],3):>7}%  {ts['pf']:>5.2f}  "
                      f"L{wr_l:>3}%({nl})  S{wr_s:>3}%({ns})  "
                      f"dPF={fmt(d_pf_t)}{flag_t}")

        # ── Analisis LONG vs SHORT por segmento ───────────────────────────────
        print(f"\n  ANALISIS LONG vs SHORT — {seg_name} (todos los TF, config optima):")
        print(f"  {'TF':>5}  {'Side':>6}  {'N':>5}  {'WR':>4}  {'Avg PnL':>8}  {'PF':>5}  Recomendacion")
        print(f"  {'-'*60}")
        for tf_key in TF_BASE:
            key = (seg_name, tf_key)
            if key not in final_recs: continue
            rec = final_recs[key]
            best = rec["best"]
            tf_cfg = TF_BASE[tf_key]
            start_dt  = (now_utc - timedelta(weeks=tf_cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
            htf_start = (now_utc - timedelta(weeks=tf_cfg["weeks"]+2)).strftime("%Y-%m-%dT%H:%M:%SZ")

            all_l, all_s = [], []
            for ticker in rec["tickers"]:
                bars     = fetch_all(ticker, tf_cfg["tf"],  start_dt)
                htf_bars = fetch_all(ticker, tf_cfg["htf"], htf_start)
                if len(bars) < tf_cfg["warmup"]+5: continue
                t = simulate(bars, htf_bars, tf_cfg["warmup"],
                             best["ms"], best["adv"],
                             best["th"], best["nb"], best["pc"], best["cr"], ticker)
                all_l += [x for x in t if x["side"]=="LONG"]
                all_s += [x for x in t if x["side"]=="SHORT"]

            sl = stats(all_l); ss = stats(all_s)
            if sl["n"] > 0:
                rec_l = "usar LONG" if sl["pf"]>1.2 else "evitar LONG"
                print(f"  {tf_key:>5}  {'LONG':>6}  {sl['n']:>5}  {sl['pct_win']:>3}%  "
                      f"{fmt(sl['avg'],3):>8}%  {sl['pf']:>5.2f}  {rec_l}")
            if ss["n"] > 0:
                rec_s = "usar SHORT" if ss["pf"]>1.2 else "evitar SHORT"
                print(f"  {tf_key:>5}  {'SHORT':>6}  {ss['n']:>5}  {ss['pct_win']:>3}%  "
                      f"{fmt(ss['avg'],3):>8}%  {ss['pf']:>5.2f}  {rec_s}")

    # ── Tabla final de recomendaciones ────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  TABLA FINAL DE PARAMETROS OPTIMOS POR SEGMENTO x TIMEFRAME")
    print(f"{'='*W}")
    print(f"  {'Segmento':>10}  {'TF':>5}  {'th':>3}  {'nb':>3}  {'pc':>3}  {'cr':>3}  "
          f"{'ms':>4}  {'adv':>5}  {'PF':>5}  {'WR':>4}  {'Avg':>7}  {'dPF':>5}")
    print(f"  {'-'*82}")
    for seg_name in SEGMENTS:
        for tf_key in TF_BASE:
            key = (seg_name, tf_key)
            if key not in final_recs: continue
            d = final_recs[key]
            b = d["best"]; bs_s = d["best"]["stats"]; base = d["baseline"]
            d_pf = bs_s["pf"] - base["pf"]
            flag = " +" if d_pf > 0.1 else (" -" if d_pf < -0.1 else "  ")
            print(f"  {seg_name:>10}  {tf_key:>5}  {b['th']:>+3}  {b['nb']:>3}  "
                  f"{b['pc']:>3}  {b['cr']:>3}  {b['ms']:>4}  {b['adv']:>5.1f}  "
                  f"{bs_s['pf']:>5.2f}  {bs_s['pct_win']:>3}%  "
                  f"{fmt(bs_s['avg'],3):>7}%  {fmt(d_pf):>5}{flag}")

    print(f"\n  INTERPRETACION CLAVE:")
    print(f"  - threshold_adj: cuantos puntos adicionales necesita este segmento para entrar")
    print(f"  - n_binary_min: minimo de criterios tecnicos (L1-L6) sin contar patrones")
    print(f"  - pattern_cap: 2=solo cuenta hasta 2pts de patron; 4=sin limite")
    print(f"  - confirm_req: 2=confirmacion estandar; 3=mas selectivo en segmentos ruidosos")
    print()
    print(f"  PROXIMOS PASOS:")
    print(f"  1. Implementar los parametros como SEGMENT_CONFIG en api/app.py")
    print(f"  2. El watcher detecta la categoria del ticker y aplica sus parametros")
    print(f"  3. Actualizar backtest_lossdna.py con segmentos para analisis mas fino")
    print()


if __name__ == "__main__":
    run()
