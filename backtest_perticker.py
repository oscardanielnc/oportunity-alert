#!/usr/bin/env python3
"""
Per-Ticker Parameter Optimization
==================================
Calibra los parametros optimos de forma individual por cada ticker x TF.
Resultado: TICKER_PARAMS dict listo para segment_config.py.

Por que esto es mejor que segmentos:
  Dentro del mismo segmento, NVDA (blue-chip $400) y QBTS (especulativo $15)
  tienen personalidades tecnicas completamente distintas. RSI 35 para NVDA
  es evento raro; para IONQ es semanal. Este script encuentra los parametros
  que se adaptan a cada ticker individualmente.

Grid (324 combos por ticker x TF):
  threshold_adj : [0, +1, +2]
  n_binary_min  : [1, 2, 3]
  pattern_cap   : [2, 4]
  confirm_req   : [2, 3]
  max_scans     : 3 valores por TF
  adverse_pct   : 3 valores por TF

Criterio de validez: minimo MIN_TRADES trades en la mejor configuracion.
  Si n < MIN_TRADES -> usar params del segmento (evitar overfitting).
  Si mejor PF en todas las combos < BLOCK_PF_THRESHOLD -> block_new=True.

Outputs:
  - Tabla detallada por ticker x TF
  - TICKER_PARAMS dict (Python) listo para copiar a segment_config.py
  - data/ticker_params.json con todos los resultados
"""
import os, sys, time as _time, json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS
from utils.segment_config import get_segment_params, TICKER_SEGMENT

ALPACA_BASE = "https://data.alpaca.markets"
LIMA_OFFSET = -5

TICKERS = ["NVDA", "AMD", "MU", "PLTR", "IONQ", "QBTS", "APP", "RDDT", "AVGO", "MSFT"]

TF_BASE = {
    "1m":  {"tf":"1Min",  "htf":"5Min",  "weeks":1,  "warmup":30,
            "max_scans_grid":[10,15,20],  "adverse_grid":[0.3,0.5,0.7]},
    "5m":  {"tf":"5Min",  "htf":"15Min", "weeks":6,  "warmup":30,
            "max_scans_grid":[8,12,16],   "adverse_grid":[0.0,0.3,0.5]},
    "15m": {"tf":"15Min", "htf":"1Hour", "weeks":12, "warmup":30,
            "max_scans_grid":[3,4,6],     "adverse_grid":[0.5,1.0,1.5]},
}

GRID = {
    "threshold_adj": [0, 1, 2],
    "n_binary_min":  [1, 2, 3],
    "pattern_cap":   [2, 4],
    "confirm_req":   [2, 3],
}

MIN_TRADES       = 15   # minimo para que los params sean estadisticamente validos
BLOCK_PF_THRESH  = 0.7  # si el mejor PF posible < 0.7 -> considerar bloquear ticker+TF


# ── API ───────────────────────────────────────────────────────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


# ── Custom entry decision ─────────────────────────────────────────────────────

def custom_entry(result, pos_state, th, nb, pc):
    if pos_state != "SIN_POSICION":
        return result["signal"]
    sig = result.get("signal","ESPERAR")
    if sig not in ("ENTRAR_LONG","ENTRAR_SHORT"):
        return sig

    crit    = result.get("criteria",{})
    htf     = result.get("htf_trend","NEUTRAL")
    is_long = (sig == "ENTRAR_LONG")

    n_bin   = crit.get("n_binary_long" if is_long else "n_binary_short", 0)
    pat_raw = crit.get("bull_pscore"   if is_long else "bear_pscore",    0)
    score   = n_bin + min(pat_raw, pc)

    if htf == "BULLISH":   thresh = max(3,4+th) if is_long else min(9,8+th)
    elif htf == "BEARISH": thresh = min(9,8+th) if is_long else max(3,4+th)
    else:                  thresh = max(3,5+th)

    return sig if (score >= thresh and n_bin >= nb) else "ESPERAR"


# ── Simulacion ────────────────────────────────────────────────────────────────

def simulate(bars, htf_bars, warmup, max_scans, adverse_pct, th, nb, pc, cr):
    cand_sig=None; cand_count=0; cur_sig="ESPERAR"
    lock_rem=0; lock_dir=None; position=None; trades=[]; htf_ptr=0

    for i in range(warmup, len(bars)):
        bar=bars[i]; cur_ts=bar["t"]; cur_price=bar["c"]
        candles=bars[max(0,i-119):i+1]

        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0,htf_ptr-50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice)>=20 else "NEUTRAL"

        pos_state = "SIN_POSICION" if not position else position["side"]

        if position and max_scans < 9999:
            position["scans_held"]=position.get("scans_held",0)+1
            ep=position["entry_price"]
            adverse=((ep-cur_price)/ep*100 if pos_state=="LONG" else (cur_price-ep)/ep*100)
            if position["scans_held"]>=max_scans and adverse>=adverse_pct:
                no=bars[i+1]["o"] if i+1<len(bars) else cur_price
                nt=lima_ts(bars[i+1]) if i+1<len(bars) else lima_ts(bar)
                pnl=((no-ep)/ep*100 if pos_state=="LONG" else (ep-no)/ep*100)
                trades.append({"side":pos_state,"in_t":position["entry_time"],"in_p":ep,
                               "out_t":nt,"out_p":no,"pnl":round(pnl,3),"note":"TS"})
                position=None;lock_dir=None;lock_rem=0
                cur_sig="ESPERAR";cand_sig=None;cand_count=0
                continue

        result  = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig = custom_entry(result, pos_state, th, nb, pc)

        if lock_rem>0 and lock_dir:
            if (lock_dir=="SHORT" and raw_sig=="ENTRAR_SHORT") or \
               (lock_dir=="LONG"  and raw_sig=="ENTRAR_LONG"):
                raw_sig="ESPERAR"
        if lock_rem>0:
            lock_rem-=1
            if lock_rem==0: lock_dir=None

        confirmed=False
        if raw_sig==cand_sig:
            cand_count+=1
            if cand_count>=cr and raw_sig!=cur_sig:
                confirmed=True; cur_sig=raw_sig
            if cand_count>=cr: cand_sig=None; cand_count=0
        else:
            cand_sig=raw_sig; cand_count=1

        if not confirmed: continue

        if cur_sig=="ENTRAR_LONG":   lock_dir="SHORT"; lock_rem=DIRECTION_LOCK_SCANS
        elif cur_sig=="ENTRAR_SHORT": lock_dir="LONG";  lock_rem=DIRECTION_LOCK_SCANS
        elif cur_sig in ("CERRAR_LONG","CERRAR_SHORT","ESPERAR"):
            lock_dir=None; lock_rem=0

        ep2=bars[i+1]["o"] if i+1<len(bars) else bar["c"]
        et2=lima_ts(bars[i+1]) if i+1<len(bars) else lima_ts(bar)

        def close_pos(ep2,et2,note="",_p=None):
            p=_p or position
            if not p: return
            side=p["side"]; pnl=(ep2-p["entry_price"])/p["entry_price"]*100
            if side=="SHORT": pnl=-pnl
            trades.append({"side":side,"in_t":p["entry_time"],"in_p":p["entry_price"],
                           "out_t":et2,"out_p":ep2,"pnl":round(pnl,3),"note":note})

        if cur_sig=="ENTRAR_LONG":
            if position and position["side"]=="SHORT": close_pos(ep2,et2,"inv",position); position=None
            if not position: position={"side":"LONG","entry_price":ep2,"entry_time":et2,"scans_held":0}
        elif cur_sig=="ENTRAR_SHORT":
            if position and position["side"]=="LONG": close_pos(ep2,et2,"inv",position); position=None
            if not position: position={"side":"SHORT","entry_price":ep2,"entry_time":et2,"scans_held":0}
        elif cur_sig=="CERRAR_LONG"  and position and position["side"]=="LONG":
            close_pos(ep2,et2,"",position); position=None
        elif cur_sig=="CERRAR_SHORT" and position and position["side"]=="SHORT":
            close_pos(ep2,et2,"",position); position=None

    if position:
        lp=bars[-1]["c"]; lt2=lima_ts(bars[-1])+"*"; side=position["side"]
        pnl=(lp-position["entry_price"])/position["entry_price"]*100
        if side=="SHORT": pnl=-pnl
        trades.append({"side":side,"in_t":position["entry_time"],"in_p":position["entry_price"],
                       "out_t":lt2,"out_p":lp,"pnl":round(pnl,3),"note":"fp"})
    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats(trades):
    if not trades:
        return {"n":0,"pct_win":0.0,"total":0.0,"avg":0.0,"pf":0.0,"best":0.0,"worst":0.0}
    pnls=[t["pnl"] for t in trades]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    gw=sum(wins); gl=abs(sum(losses))
    pf=round(gw/gl,2) if gl>0 else (99.0 if gw>0 else 0.0)
    return {"n":len(trades),"pct_win":round(len(wins)/len(trades)*100),
            "total":round(sum(pnls),2),"avg":round(sum(pnls)/len(trades),3),
            "pf":pf,"best":round(max(wins,default=0.0),2),
            "worst":round(min(losses,default=0.0),2)}

def composite_score(s, min_trades=MIN_TRADES):
    if s["n"] < min_trades: return -999.0
    return round(0.5*min(s["pf"],6.0)/6.0*5 + 0.3*(s["pct_win"]/100)*5 +
                 0.2*min(max(s["avg"]*15,0),5), 4)

def fmt(v,d=2): return f"{'+' if v>=0 else ''}{v:.{d}f}"


# ── Runner ────────────────────────────────────────────────────────────────────

def run():
    W = 96
    now_utc = datetime.now(timezone.utc)
    label   = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*W}")
    print(f"  PER-TICKER PARAMETER OPTIMIZATION  --  {label} Lima")
    print(f"  Tickers ({len(TICKERS)}): {', '.join(TICKERS)}")
    print(f"  Grid: 324 combos x 10 tickers x 3 TFs = {324*len(TICKERS)*3} simulaciones")
    print(f"  Validez: min {MIN_TRADES} trades | block si mejor PF < {BLOCK_PF_THRESH}")
    print(f"{'='*W}")

    # Resultados finales: (ticker, tf) -> best_params o "USE_SEGMENT" o "BLOCK"
    ticker_params_out = {}   # para segment_config.py
    all_results = {}         # para analisis y JSON

    # ── Pre-fetch todos los datos ─────────────────────────────────────────────
    ticker_data = {}  # ticker -> {tf -> (bars, htf_bars)}
    print("\n  Pre-cargando datos para todos los tickers y TFs...")
    for ticker in TICKERS:
        ticker_data[ticker] = {}
        for tf_key, cfg in TF_BASE.items():
            start_dt  = (now_utc - timedelta(weeks=cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
            htf_start = (now_utc - timedelta(weeks=cfg["weeks"]+2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars     = fetch_all(ticker, cfg["tf"],  start_dt)
            htf_bars = fetch_all(ticker, cfg["htf"], htf_start)
            if len(bars) >= cfg["warmup"]+5:
                ticker_data[ticker][tf_key] = (bars, htf_bars)
        loaded = list(ticker_data[ticker].keys())
        print(f"    {ticker}: {', '.join(f'{k}({len(ticker_data[ticker][k][0])}b)' for k in loaded)}")

    print()

    # ── Grid search por ticker x TF ───────────────────────────────────────────
    for ticker in TICKERS:
        seg = TICKER_SEGMENT.get(ticker, "DEFAULT")
        print(f"\n{'#'*W}")
        print(f"  TICKER: {ticker}  [segmento actual: {seg}]")
        print(f"{'#'*W}")

        all_results[ticker] = {}

        for tf_key, cfg in TF_BASE.items():
            if tf_key not in ticker_data[ticker]:
                print(f"  [{tf_key}] Sin datos — skip")
                continue

            bars, htf_bars = ticker_data[ticker][tf_key]

            # Baseline: params actuales del segmento
            seg_p   = get_segment_params(ticker, tf_key)
            base_ms  = seg_p["max_scans"]   if seg_p["max_scans"]   is not None else cfg["max_scans_grid"][1]
            base_adv = seg_p["adverse_pct"] if seg_p["adverse_pct"] is not None else cfg["adverse_grid"][1]
            base_th  = seg_p["threshold_adj"]
            base_nb  = seg_p["n_binary_min"]
            base_pc  = seg_p["pattern_cap"]
            base_cr  = seg_p["confirm_req"]

            base_trades = simulate(bars, htf_bars, cfg["warmup"], base_ms, base_adv,
                                   base_th, base_nb, base_pc, base_cr)
            base_s = stats(base_trades)

            # Grid
            results = []
            n_combos = len(GRID["threshold_adj"]) * len(GRID["n_binary_min"]) * \
                       len(GRID["pattern_cap"])    * len(GRID["confirm_req"])   * \
                       len(cfg["max_scans_grid"])  * len(cfg["adverse_grid"])

            sys.stdout.write(f"  [{tf_key}] Baseline (seg): n={base_s['n']} WR={base_s['pct_win']}% "
                             f"PF={base_s['pf']:.2f}  |  Corriendo {n_combos} combos... ")
            sys.stdout.flush()

            for th in GRID["threshold_adj"]:
                for nb in GRID["n_binary_min"]:
                    for pc in GRID["pattern_cap"]:
                        for cr in GRID["confirm_req"]:
                            for ms in cfg["max_scans_grid"]:
                                for adv in cfg["adverse_grid"]:
                                    trades = simulate(bars, htf_bars, cfg["warmup"],
                                                      ms, adv, th, nb, pc, cr)
                                    s  = stats(trades)
                                    cs = composite_score(s)
                                    results.append({"th":th,"nb":nb,"pc":pc,"cr":cr,
                                                    "ms":ms,"adv":adv,"stats":s,"score":cs})

            results.sort(key=lambda x: -x["score"])
            best = results[0]
            bs   = best["stats"]

            # Verificar si hay alguna combo viable (n >= MIN_TRADES)
            viable = [r for r in results if r["stats"]["n"] >= MIN_TRADES]
            best_viable_pf = viable[0]["stats"]["pf"] if viable else 0.0

            # Determinar veredicto
            if not viable:
                verdict = "USE_SEGMENT"
                verdict_label = f"POCOS_DATOS (max n={max(r['stats']['n'] for r in results)} < {MIN_TRADES})"
            elif best_viable_pf < BLOCK_PF_THRESH:
                verdict = "BLOCK"
                verdict_label = f"BLOQUEAR (mejor PF={best_viable_pf:.2f} < {BLOCK_PF_THRESH})"
            else:
                verdict = "CUSTOM"
                verdict_label = "PARAMETROS_PROPIOS"

            print(f"OK  -> {verdict_label}")

            # Mostrar top 5 combos viables
            if viable:
                print(f"  [{tf_key}] Top 5 combos validos (n>={MIN_TRADES}):")
                print(f"  {'R':>3}  {'th':>3}  {'nb':>3}  {'pc':>3}  {'cr':>3}  {'ms':>4}  {'adv':>5}"
                      f"  {'N':>5}  {'WR':>4}  {'PnL':>8}  {'Avg':>7}  {'PF':>5}  {'Score':>6}  vs_seg")
                print(f"  {'-'*84}")
                for rank, r in enumerate(viable[:5], 1):
                    s  = r["stats"]
                    d_pf = s["pf"] - base_s["pf"]
                    flag = " <<" if rank==1 else ""
                    print(f"  {rank:>3}  {r['th']:>+3}  {r['nb']:>3}  {r['pc']:>3}  {r['cr']:>3}  "
                          f"{r['ms']:>4}  {r['adv']:>5.1f}  {s['n']:>5}  {s['pct_win']:>3}%  "
                          f"{fmt(s['total']):>8}%  {fmt(s['avg'],3):>7}%  {s['pf']:>5.2f}  "
                          f"{r['score']:>6.3f}  dPF={fmt(d_pf)}{flag}")
            else:
                print(f"  [{tf_key}] Ningun combo supera min_trades={MIN_TRADES}")

            # Guardar resultado
            best_v = viable[0] if viable else None
            all_results[ticker][tf_key] = {
                "verdict":    verdict,
                "best_viable": best_v,
                "baseline_s": base_s,
                "seg_params": seg_p,
                "viable_count": len(viable),
            }

            # Construir TICKER_PARAMS entry
            key = (ticker, tf_key)
            if verdict == "CUSTOM" and best_v:
                bv = best_v
                ticker_params_out[key] = {
                    "threshold_adj": bv["th"],
                    "n_binary_min":  bv["nb"],
                    "pattern_cap":   bv["pc"],
                    "confirm_req":   bv["cr"],
                    "max_scans":     bv["ms"],
                    "adverse_pct":   bv["adv"],
                    "block_new":     False,
                    "block_short":   False,
                }
                # Heredar block_short del segmento si el segmento lo tenia
                if seg_p.get("block_short"):
                    ticker_params_out[key]["block_short"] = True
            elif verdict == "BLOCK":
                ticker_params_out[key] = {**get_segment_params(ticker, tf_key),
                                           "block_new": True}

    # ── Resumen global ────────────────────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  RESUMEN GLOBAL -- Veredictos por ticker x TF")
    print(f"{'='*W}")
    print(f"  {'Ticker':>7}  {'Seg':>10}  {'1m':>12}  {'5m':>12}  {'15m':>12}")
    print(f"  {'-'*60}")
    for ticker in TICKERS:
        seg = TICKER_SEGMENT.get(ticker,"DEFAULT")
        row = []
        for tf in ["1m","5m","15m"]:
            res = all_results.get(ticker,{}).get(tf,{})
            v = res.get("verdict","NO_DATA")
            bv = res.get("best_viable")
            if v == "CUSTOM" and bv:
                n = bv["stats"]["n"]; pf = bv["stats"]["pf"]
                row.append(f"OK n={n} PF={pf:.2f}")
            elif v == "BLOCK":
                bpf = bv["stats"]["pf"] if bv else 0
                row.append(f"BLOCK PF={bpf:.2f}")
            elif v == "USE_SEGMENT":
                row.append("USE_SEG")
            else:
                row.append("NO_DATA")
        print(f"  {ticker:>7}  {seg:>10}  {row[0]:>12}  {row[1]:>12}  {row[2]:>12}")

    # ── Mejora esperada vs segmento ───────────────────────────────────────────
    print(f"\n  MEJORA vs PARAMS DE SEGMENTO (solo combos validos):")
    print(f"  {'Ticker':>7}  {'TF':>4}  {'PF_seg':>7}  {'PF_opt':>7}  {'dPF':>6}  "
          f"{'WR_seg':>7}  {'WR_opt':>7}  Veredicto")
    print(f"  {'-'*70}")
    for ticker in TICKERS:
        for tf in ["1m","5m","15m"]:
            res = all_results.get(ticker,{}).get(tf,{})
            v = res.get("verdict","NO_DATA")
            bv = res.get("best_viable")
            bs = res.get("baseline_s",{})
            if v == "CUSTOM" and bv:
                s = bv["stats"]
                d_pf = s["pf"] - bs.get("pf",0)
                d_wr = s["pct_win"] - bs.get("pct_win",0)
                flag = " ++ MEJOR" if d_pf > 0.2 else (" ~ igual" if abs(d_pf)<=0.2 else " -- PEOR")
                print(f"  {ticker:>7}  {tf:>4}  {bs.get('pf',0):>7.2f}  {s['pf']:>7.2f}  "
                      f"{fmt(d_pf):>6}  {bs.get('pct_win',0):>6}%  {s['pct_win']:>6}%  {flag}")
            elif v == "BLOCK":
                print(f"  {ticker:>7}  {tf:>4}  {'':>7}  {'BLOCK':>7}  {'':>6}  "
                      f"{'':>7}  {'':>7}  DESACTIVAR este TF")
            elif v == "USE_SEGMENT":
                n_max = max((r["stats"]["n"] for r in []), default=0)
                print(f"  {ticker:>7}  {tf:>4}  {'':>7}  {'USE_SEG':>7}  {'':>6}  "
                      f"{'':>7}  {'':>7}  Pocos datos, usar segmento")

    # ── Generar TICKER_PARAMS Python dict ─────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  TICKER_PARAMS -- COPIAR A utils/segment_config.py")
    print(f"  (reemplaza 'USE_SEGMENT' con los params del segmento al copiar)")
    print(f"{'='*W}")
    print()
    print("TICKER_PARAMS = {")
    for ticker in TICKERS:
        seg = TICKER_SEGMENT.get(ticker,"DEFAULT")
        printed_ticker = False
        for tf in ["1m","5m","15m"]:
            key = (ticker, tf)
            if key in ticker_params_out:
                if not printed_ticker:
                    print(f"    # {ticker} [{seg}]")
                    printed_ticker = True
                p = ticker_params_out[key]
                block_tag = " # DESACTIVADO" if p.get("block_new") else ""
                print(f"    (\"{ticker}\", \"{tf}\"): "
                      f"{{\"threshold_adj\":{p['threshold_adj']}, "
                      f"\"n_binary_min\":{p['n_binary_min']}, "
                      f"\"pattern_cap\":{p['pattern_cap']}, "
                      f"\"confirm_req\":{p['confirm_req']}, "
                      f"\"max_scans\":{p['max_scans']}, "
                      f"\"adverse_pct\":{p['adverse_pct']}, "
                      f"\"block_new\":{p['block_new']}, "
                      f"\"block_short\":{p['block_short']}}},{block_tag}")
        if printed_ticker: print()
    print("}")
    print()

    # ── Guardar JSON ──────────────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    json_path = os.path.join("data", "ticker_params.json")
    json_out = {}
    for (ticker, tf), p in ticker_params_out.items():
        json_out[f"{ticker}_{tf}"] = p
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_out, f, indent=2)
    print(f"  Guardado: {json_path}")
    print()


if __name__ == "__main__":
    run()
