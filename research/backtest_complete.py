import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
Backtest Completo — replica EXACTAMENTE la logica del watcher live.
====================================================================
El super_backtest.py solo testea compute_signal() en aislamiento.
Este script aplica TODOS los filtros del sistema real:

  1. Segment entry decision  (block_new, block_short, threshold_adj, n_binary_min, pattern_cap)
  2. Session threshold       (03-08h Lima → 6, 15-17h Lima → 6, resto → 5)
  3. Ticker threshold        (RDDT=7, etc.)
  4. QQQ macro filter        (5m: QQQ BEARISH → LONG +2pts)
  5. Direction lock          (DIRECTION_LOCK_SCANS)
  6. Confirm_req per-segment (LARGE_CAP 1m=3, AI_SW 5m/15m=3, resto=2)
  7. Time-stop per-segment   (max_scans y adverse_pct de SEGMENT_PARAMS)

Los session thresholds usan la hora Lima del timestamp de cada barra
en lugar del reloj real, para ser 100% reproducible en historico.
"""
import os, sys, time as _time, bisect
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS
from utils.segment_config import (
    TIMESTOP_SCANS, TIMESTOP_ADVERSE_PCT,
    TICKER_THRESHOLDS, get_segment_params, get_session_threshold_for_hour,
)

ALPACA_BASE = "https://data.alpaca.markets"
LIMA_OFFSET = -5
QQQ_MACRO_ADJ = 2   # QQQ+2 para 5m

TICKERS = ["NVDA", "AMD", "MU", "PLTR", "IONQ", "QBTS", "APP", "RDDT", "AVGO", "MSFT"]

TF_CONFIG = {
    "1m":  {"tf":"1Min",  "htf":"5Min",  "macro_tf":None,   "weeks":1,  "warmup":30},
    "5m":  {"tf":"5Min",  "htf":"15Min", "macro_tf":"15Min", "weeks":6,  "warmup":30},
    "15m": {"tf":"15Min", "htf":"1Hour", "macro_tf":None,    "weeks":12, "warmup":30},
}


# -- API -----------------------------------------------------------------------

def _hdrs():
    return {"APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY",""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY","")}

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


# -- Helpers de tiempo ---------------------------------------------------------

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")

def lima_hour(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).hour

def is_regular_session(bar):
    """True si la barra es de sesion regular (09:30-16:00 ET)."""
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    # ET = UTC-4 (EDT) o UTC-5 (EST); usamos Lima offset como proxy (ambos ~-5/-4)
    et = dt + timedelta(hours=-4)  # asumir EDT (mas comun en periodo de prueba)
    dm = et.hour * 60 + et.minute
    return 9*60+30 <= dm < 16*60


# -- QQQ macro index -----------------------------------------------------------

def build_macro_index(spy_bars):
    """Precomputa trend de QQQ/SPY para lookup O(1) por timestamp."""
    index = []
    for i in range(26, len(spy_bars)):
        t       = spy_bars[i]["t"]
        window  = spy_bars[max(0, i-49):i+1]
        trend   = compute_htf_trend(window)
        index.append((t, trend))
    return index

def get_macro_trend_at(index, cur_t):
    if not index: return "NEUTRAL"
    ts_list = [x[0] for x in index]
    idx = bisect.bisect_right(ts_list, cur_t) - 1
    return index[idx][1] if idx >= 0 else "NEUTRAL"


# -- Entry filter — replica _segment_entry_decision de api/app.py -------------

def segment_entry(result, ticker, interval):
    params  = get_segment_params(ticker, interval)
    if params["block_new"]:
        return "ESPERAR"
    sig = result.get("signal","ESPERAR")
    if sig not in ("ENTRAR_LONG","ENTRAR_SHORT"):
        return sig
    if params["block_short"] and sig == "ENTRAR_SHORT":
        return "ESPERAR"

    crit    = result.get("criteria",{})
    htf     = result.get("htf_trend","NEUTRAL")
    is_long = (sig == "ENTRAR_LONG")
    th, nb, pc = params["threshold_adj"], params["n_binary_min"], params["pattern_cap"]

    n_bin   = crit.get("n_binary_long" if is_long else "n_binary_short", 0)
    pat_raw = crit.get("bull_pscore"   if is_long else "bear_pscore",    0)
    score   = n_bin + min(pat_raw, pc)

    if htf == "BULLISH":
        thresh = max(3, 4+th) if is_long else min(9, 8+th)
    elif htf == "BEARISH":
        thresh = min(9, 8+th) if is_long else max(3, 4+th)
    else:
        thresh = max(3, 5+th)

    return sig if (score >= thresh and n_bin >= nb) else "ESPERAR"


def apply_qqq_macro(result, qqq_trend, adj=2):
    if qqq_trend == "NEUTRAL": return result["signal"]
    sl, ss = result["score_long"], result["score_short"]
    htf    = result.get("htf_trend","NEUTRAL")
    if htf == "BULLISH":   lt, st = 4, 8
    elif htf == "BEARISH": lt, st = 8, 4
    else:                  lt, st = 5, 5
    if qqq_trend == "BEARISH": lt += adj
    elif qqq_trend == "BULLISH": st += adj
    if sl >= lt: return "ENTRAR_LONG"
    if ss >= st: return "ENTRAR_SHORT"
    return "ESPERAR"


# -- Simulacion completa -------------------------------------------------------

def simulate_complete(bars, htf_bars, ticker, interval, warmup,
                      qqq_macro_index=None):
    """
    Simulacion barra a barra que aplica TODOS los filtros del watcher live.
    """
    params      = get_segment_params(ticker, interval)
    confirm_req = params["confirm_req"]
    max_scans   = params["max_scans"]   if params["max_scans"]   is not None else TIMESTOP_SCANS.get(interval, 20)
    adverse_pct = params["adverse_pct"] if params["adverse_pct"] is not None else TIMESTOP_ADVERSE_PCT.get(interval, 0.5)

    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []
    htf_ptr    = 0
    filters    = {"seg":0,"session":0,"ticker_th":0,"qqq":0,"dir_lock":0}

    for i in range(warmup, len(bars)):
        bar       = bars[i]
        cur_ts    = bar["t"]
        cur_price = bar["c"]
        candles   = bars[max(0, i-119):i+1]
        lh        = lima_hour(bar)
        is_reg    = is_regular_session(bar)

        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0, htf_ptr-50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"

        pos_state = "SIN_POSICION" if not position else position["side"]

        # -- Time-stop ----------------------------------------------------------
        if position and max_scans < 9999:
            position["scans_held"] = position.get("scans_held",0) + 1
            ep_pos  = position["entry_price"]
            adverse = ((ep_pos-cur_price)/ep_pos*100 if pos_state=="LONG"
                       else (cur_price-ep_pos)/ep_pos*100)
            if position["scans_held"] >= max_scans and adverse >= adverse_pct:
                no   = bars[i+1]["o"] if i+1 < len(bars) else cur_price
                nt   = lima_ts(bars[i+1]) if i+1 < len(bars) else lima_ts(bar)
                pnl  = ((no-ep_pos)/ep_pos*100 if pos_state=="LONG" else (ep_pos-no)/ep_pos*100)
                trades.append({"ticker":ticker,"side":pos_state,
                               "in_t":position["entry_time"],"in_p":ep_pos,
                               "out_t":nt,"out_p":no,"pnl":round(pnl,3),"note":"TS"})
                position=None; lock_dir=None; lock_rem=0
                cur_sig="ESPERAR"; cand_sig=None; cand_count=0
                continue

        result  = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig = result["signal"]

        # -- Filter 1: Segment entry decision ----------------------------------
        if pos_state == "SIN_POSICION" and raw_sig in ("ENTRAR_LONG","ENTRAR_SHORT"):
            new = segment_entry(result, ticker, interval)
            if new != raw_sig: filters["seg"] += 1
            raw_sig = new

        # -- Filter 2: Session threshold ----------------------------------------
        if raw_sig in ("ENTRAR_LONG","ENTRAR_SHORT"):
            sess_thresh = get_session_threshold_for_hour(lh, is_reg, interval)
            sc = result["score_long"] if raw_sig=="ENTRAR_LONG" else result["score_short"]
            if sc < sess_thresh:
                filters["session"] += 1
                raw_sig = "ESPERAR"

        # -- Filter 3: Ticker threshold -----------------------------------------
        if raw_sig in ("ENTRAR_LONG","ENTRAR_SHORT"):
            tk_min = TICKER_THRESHOLDS.get(ticker)
            if tk_min:
                sc = result["score_long"] if raw_sig=="ENTRAR_LONG" else result["score_short"]
                if sc < tk_min:
                    filters["ticker_th"] += 1
                    raw_sig = "ESPERAR"

        # -- Filter 4: QQQ macro (solo 5m) -------------------------------------
        if interval == "5m" and raw_sig in ("ENTRAR_LONG","ENTRAR_SHORT") and qqq_macro_index:
            qt = get_macro_trend_at(qqq_macro_index, cur_ts)
            if qt != "NEUTRAL":
                new = apply_qqq_macro(result, qt, QQQ_MACRO_ADJ)
                if new != raw_sig: filters["qqq"] += 1
                raw_sig = new

        # -- Filter 5: Direction lock -------------------------------------------
        if lock_rem > 0 and lock_dir:
            if (lock_dir=="SHORT" and raw_sig=="ENTRAR_SHORT") or \
               (lock_dir=="LONG"  and raw_sig=="ENTRAR_LONG"):
                filters["dir_lock"] += 1
                raw_sig = "ESPERAR"
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0: lock_dir = None

        # -- Confirmation (per-segment confirm_req) -----------------------------
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
            pnl  = (ep2-p["entry_price"])/p["entry_price"]*100
            if side=="SHORT": pnl=-pnl
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

    return trades, filters


# -- Estadisticas --------------------------------------------------------------

def stats(trades):
    if not trades:
        return {"n":0,"wins":0,"losses":0,"pct_win":0.0,"total":0.0,
                "avg":0.0,"pf":0.0,"best":0.0,"worst":0.0,"tstops":0}
    pnls=[t["pnl"] for t in trades]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    gw=sum(wins); gl=abs(sum(losses))
    pf=round(gw/gl,2) if gl>0 else (99.0 if gw>0 else 0.0)
    return {"n":len(trades),"wins":len(wins),"losses":len(losses),
            "pct_win":round(len(wins)/len(trades)*100),
            "total":round(sum(pnls),2),"avg":round(sum(pnls)/len(trades),3),
            "pf":pf,"best":round(max(wins,default=0.0),2),
            "worst":round(min(losses,default=0.0),2),
            "tstops":sum(1 for t in trades if t.get("note")=="TS")}

def fmt(v, d=2): return f"{'+' if v>=0 else ''}{v:.{d}f}"


# -- Runner --------------------------------------------------------------------

def run():
    W = 92
    now_utc = datetime.now(timezone.utc)
    label   = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*W}")
    print(f"  BACKTEST COMPLETO — {label} Lima")
    print(f"  Replica EXACTA del watcher live con todos los filtros:")
    print(f"  Segment params | Session threshold | Ticker threshold | QQQ macro | confirm_req")
    print(f"  Tickers: {', '.join(TICKERS)}")
    print(f"{'='*W}")

    all_filters_total = {"seg":0,"session":0,"ticker_th":0,"qqq":0,"dir_lock":0}

    for tf_key, tf_cfg in TF_CONFIG.items():
        print(f"\n{'-'*W}")
        start_dt  = (now_utc - timedelta(weeks=tf_cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start = (now_utc - timedelta(weeks=tf_cfg["weeks"]+2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch QQQ macro si aplica
        qqq_index = None
        if tf_cfg["macro_tf"]:
            sys.stdout.write(f"  [{tf_key.upper()}] Cargando QQQ {tf_cfg['macro_tf']}... ")
            sys.stdout.flush()
            qqq_bars = fetch_all("QQQ", tf_cfg["macro_tf"], htf_start)
            qqq_index = build_macro_index(qqq_bars)
            print(f"{len(qqq_bars)} barras, {len(qqq_index)} puntos macro")

        print(f"  [{tf_key.upper()}] Simulando {len(TICKERS)} tickers...")
        print(f"  {'Ticker':>7}  {'Seg':>9}  {'cr':>3}  {'ms':>4}  {'N':>5}  {'WR':>4}  "
              f"{'PnL':>8}  {'Avg':>7}  {'PF':>5}  Filtros bloqueados")
        print(f"  {'-'*80}")

        all_tf_trades = []
        all_tf_filters = {"seg":0,"session":0,"ticker_th":0,"qqq":0,"dir_lock":0}

        for ticker in TICKERS:
            bars     = fetch_all(ticker, tf_cfg["tf"],  start_dt)
            htf_bars = fetch_all(ticker, tf_cfg["htf"], htf_start)
            if len(bars) < tf_cfg["warmup"]+5:
                print(f"  {ticker:>7}  skip ({len(bars)} barras)")
                continue

            from utils.segment_config import TICKER_SEGMENT
            seg  = TICKER_SEGMENT.get(ticker, "DEFAULT")
            sp   = get_segment_params(ticker, tf_key)
            cr   = sp["confirm_req"]
            ms   = sp["max_scans"]   if sp["max_scans"]   is not None else TIMESTOP_SCANS.get(tf_key)
            adv  = sp["adverse_pct"] if sp["adverse_pct"] is not None else TIMESTOP_ADVERSE_PCT.get(tf_key)
            blk  = "BLOCKED" if sp["block_new"] else ("NO-SHORT" if sp["block_short"] else "")

            trades, filters = simulate_complete(
                bars, htf_bars, ticker, tf_key, tf_cfg["warmup"], qqq_index)

            s = stats(trades)
            all_tf_trades += trades
            for k in all_tf_filters: all_tf_filters[k] += filters.get(k,0)

            filt_str = " | ".join(f"{k}={v}" for k,v in filters.items() if v>0)
            blk_tag  = f" [{blk}]" if blk else ""
            print(f"  {ticker:>7}  {seg:>9}  {cr:>3}  {ms:>4}  {s['n']:>5}  "
                  f"{s['pct_win']:>3}%  {fmt(s['total']):>8}%  {fmt(s['avg'],3):>7}%  "
                  f"{s['pf']:>5.2f}  {filt_str}{blk_tag}")

        s_all = stats(all_tf_trades)
        print(f"  {'-'*80}")
        filt_total = " | ".join(f"{k}={v}" for k,v in all_tf_filters.items() if v>0)
        print(f"  {'TOTAL':>7}  {'':>9}  {'':>3}  {'':>4}  {s_all['n']:>5}  "
              f"{s_all['pct_win']:>3}%  {fmt(s_all['total']):>8}%  {fmt(s_all['avg'],3):>7}%  "
              f"{s_all['pf']:>5.2f}  Filtros: {filt_total}")

        for k in all_filters_total: all_filters_total[k] += all_tf_filters.get(k,0)

        # Peores trades
        worst5 = sorted(all_tf_trades, key=lambda t: t["pnl"])[:5]
        print(f"\n  Peores 5 trades [{tf_key.upper()}]:")
        for t in worst5:
            seg_t = TICKER_SEGMENT.get(t["ticker"],"?")
            print(f"    {t['ticker']:>5} {t['side']:<5} {t['in_t']}->{t['out_t']}"
                  f"  ${t['in_p']:.2f}->${t['out_p']:.2f}  {t['pnl']:+.3f}%"
                  f"  [{t.get('note','')}] ({seg_t})")

    # Tabla final
    print(f"\n{'='*W}")
    print(f"  RESUMEN GLOBAL — sistema completo con todos los filtros activos")
    print(f"  Entradas bloqueadas por filtro: {' | '.join(f'{k}={v}' for k,v in all_filters_total.items())}")
    print()
    print(f"  COMPARACION vs super_backtest.py (que NO aplica filtros de watcher):")
    print(f"  El super_backtest muestra el signal engine en aislamiento.")
    print(f"  Este backtest muestra el sistema real tal como opera en produccion.")
    print()


if __name__ == "__main__":
    run()
