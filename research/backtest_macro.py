import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
Macro Context Backtest — SPY/QQQ como filtro adicional de contexto de mercado
=============================================================================
Pregunta: agregar el estado del mercado (SPY/QQQ) como un filtro de entrada
sube el win rate y profit factor del sistema?

Logica del filtro:
  - Se calcula el trend de SPY y/o QQQ en el mismo TF que el HTF del ticker
    (5m ticker -> macro 15m | 15m ticker -> macro 1h)
  - Si el mercado esta BEARISH, se eleva el umbral de entrada LONG en +adj puntos
  - Si el mercado esta BULLISH, se eleva el umbral de entrada SHORT en +adj puntos
  - Los umbrales de salida (CERRAR) no se tocan

Configuraciones testeadas (10 + BASELINE):
  SPY+1, SPY+2, SPY+3       => SPY solo, threshold +1/+2/+3
  QQQ+1, QQQ+2, QQQ+3       => QQQ solo, threshold +1/+2/+3
  AND+1, AND+2               => SPY Y QQQ deben coincidir, threshold +1/+2
  OR+1, OR+2                 => cualquiera de los dos, threshold +1/+2

Timeframes: 5m (6 semanas) y 15m (12 semanas)
Time-stop: parametros actuales del sistema (5m: 12sc/0.5%, 15m: 6sc/0.5%)
"""
import os, sys, time as _time, bisect
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

# (label, source, adj)
# source: "SPY" | "QQQ" | "AND" (ambos deben coincidir) | "OR" (cualquiera)
# adj: puntos extra al umbral de entrada contra la direccion del mercado
MACRO_CONFIGS = [
    ("SPY+1",  "SPY", 1),
    ("SPY+2",  "SPY", 2),
    ("SPY+3",  "SPY", 3),
    ("QQQ+1",  "QQQ", 1),
    ("QQQ+2",  "QQQ", 2),
    ("QQQ+3",  "QQQ", 3),
    ("AND+1",  "AND", 1),
    ("AND+2",  "AND", 2),
    ("OR+1",   "OR",  1),
    ("OR+2",   "OR",  2),
]

# Timeframe config: tf = ticker timeframe, macro_tf = timeframe para SPY/QQQ
TF_CONFIG = {
    "5m": {
        "tf":       "5Min",  "htf":      "15Min",
        "macro_tf": "15Min",             # macro a mismo nivel que HTF del ticker
        "weeks":    6,       "warmup":   30,
        "max_scans": 12,     "adverse":  0.5,  # time-stop actual del sistema
        "label": "5 min | 6 semanas | macro=15m",
    },
    "15m": {
        "tf":       "15Min", "htf":      "1Hour",
        "macro_tf": "1Hour",             # macro a mismo nivel que HTF del ticker
        "weeks":    12,      "warmup":   30,
        "max_scans": 6,      "adverse":  0.5,  # time-stop actual del sistema
        "label": "15 min | 12 semanas | macro=1h",
    },
}


# -- API --------------------------------------------------------------------------

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


# -- Macro index ------------------------------------------------------------------

def build_macro_index(spy_bars, qqq_bars):
    """
    Precomputa (timestamp, spy_trend, qqq_trend) para cada barra de SPY.
    Usa ventana deslizante de 50 barras para compute_htf_trend.
    Retorna lista ordenada para busqueda binaria.
    """
    if len(spy_bars) < 26:
        return []

    # QQQ timestamps sorted for binary search
    qqq_ts = [b["t"] for b in qqq_bars]

    index = []
    for i in range(26, len(spy_bars)):
        t = spy_bars[i]["t"]
        spy_window = spy_bars[max(0, i - 49):i + 1]
        spy_trend  = compute_htf_trend(spy_window)

        # Find QQQ bars up to this timestamp
        qi = bisect.bisect_right(qqq_ts, t) - 1
        if qi >= 26:
            qqq_window = qqq_bars[max(0, qi - 49):qi + 1]
            qqq_trend  = compute_htf_trend(qqq_window)
        else:
            qqq_trend = "NEUTRAL"

        index.append((t, spy_trend, qqq_trend))

    return index


def get_macro_trend(macro_index, cur_t, source):
    """Retorna el trend macro mas reciente antes de cur_t segun la fuente."""
    if not macro_index:
        return "NEUTRAL"
    ts_list = [x[0] for x in macro_index]
    idx = bisect.bisect_right(ts_list, cur_t) - 1
    if idx < 0:
        return "NEUTRAL"
    _, spy_t, qqq_t = macro_index[idx]

    if source == "SPY":
        return spy_t
    elif source == "QQQ":
        return qqq_t
    elif source == "AND":
        # Solo actua si ambos coinciden en una direccion no-NEUTRAL
        return spy_t if (spy_t == qqq_t and spy_t != "NEUTRAL") else "NEUTRAL"
    elif source == "OR":
        # Actua si cualquiera tiene senal; si discrepan, NEUTRAL
        if spy_t == qqq_t:
            return spy_t
        if spy_t != "NEUTRAL" and qqq_t == "NEUTRAL":
            return spy_t
        if qqq_t != "NEUTRAL" and spy_t == "NEUTRAL":
            return qqq_t
        return "NEUTRAL"  # discrepancia BULLISH vs BEARISH
    return "NEUTRAL"


def apply_macro_filter(res, macro_trend, adj):
    """
    Reaplica la logica de decision de entrada con umbral ajustado por macro.
    Solo afecta entradas (SIN_POSICION). Salidas no se modifican.
    Retorna el signal ajustado.
    """
    if macro_trend == "NEUTRAL" or adj == 0:
        return res["signal"]

    score_long  = res["score_long"]
    score_short = res["score_short"]
    htf         = res.get("htf_trend", "NEUTRAL")

    # Umbrales base (misma logica que signal_engine.py lines 536-540)
    if htf == "BULLISH":
        long_thresh, short_thresh = 4, 8
    elif htf == "BEARISH":
        long_thresh, short_thresh = 8, 4
    else:
        long_thresh, short_thresh = 5, 5

    # Ajuste macro: mas dificil entrar contra la direccion del mercado
    if macro_trend == "BEARISH":
        long_thresh  += adj  # mercado cae -> LONG mas dificil
    elif macro_trend == "BULLISH":
        short_thresh += adj  # mercado sube -> SHORT mas dificil

    if score_long  >= long_thresh:  return "ENTRAR_LONG"
    if score_short >= short_thresh: return "ENTRAR_SHORT"
    return "ESPERAR"


# -- Simulacion -------------------------------------------------------------------

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


def simulate(bars, htf_bars, warmup, max_scans, adverse_pct,
             macro_index=None, macro_source=None, macro_adj=0):
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []
    filtered   = 0
    htf_ptr    = 0  # pointer O(1) amortizado para HTF lookup

    for i in range(warmup, len(bars)):
        bar       = bars[i]
        cur_ts    = bar["t"]
        cur_price = bar["c"]

        # Ventana fija de 120 barras (EMA26 converge, S/R usa last-50)
        candles = bars[max(0, i - 119):i + 1]

        # Avanzar pointer HTF
        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0, htf_ptr - 50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"

        # -- Time-stop ----------------------------------------------------------
        if position and max_scans < 9999:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep_pos  = position["entry_price"]
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

        # -- Macro filter (solo para nuevas entradas) ---------------------------
        if (pos_state == "SIN_POSICION" and macro_index is not None
                and macro_source and macro_adj > 0):
            macro_trend  = get_macro_trend(macro_index, cur_ts, macro_source)
            raw_sig_adj  = apply_macro_filter(res, macro_trend, macro_adj)
            if raw_sig_adj != raw_sig and raw_sig in ("ENTRAR_LONG", "ENTRAR_SHORT"):
                filtered += 1
            raw_sig = raw_sig_adj

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

    return trades, filtered


# -- Estadisticas -----------------------------------------------------------------

def stats(trades):
    if not trades:
        return {"n": 0, "wins": 0, "losses": 0, "pct_win": 0.0,
                "total": 0.0, "avg": 0.0, "pf": 0.0,
                "best": 0.0, "worst": 0.0, "tstops": 0}
    pnls       = [t["pnl"] for t in trades]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]
    total      = sum(pnls)
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    tstops = sum(1 for t in trades if t.get("note") == "TIME-STOP")
    return {
        "n":       len(trades),
        "wins":    len(wins),
        "losses":  len(losses),
        "pct_win": round(len(wins) / len(trades) * 100),
        "total":   round(total, 2),
        "avg":     round(total / len(trades), 3),
        "pf":      pf,
        "best":    round(max(wins,   default=0.0), 2),
        "worst":   round(min(losses, default=0.0), 2),
        "tstops":  tstops,
    }


def fmt(v, decimals=2, sign=True):
    return f"{'+' if sign and v >= 0 else ''}{v:.{decimals}f}"


# -- Macro stats -------------------------------------------------------------------

def macro_distribution(macro_index):
    """Cuenta cuantas barras en cada estado macro."""
    counts = {"BULLISH": 0, "NEUTRAL": 0, "BEARISH": 0}
    for _, spy_t, qqq_t in macro_index:
        counts[spy_t] = counts.get(spy_t, 0) + 1
    total = len(macro_index) or 1
    return {k: (v, round(v / total * 100)) for k, v in counts.items()}


# -- Runner -----------------------------------------------------------------------

def run():
    W = 100
    now_utc = datetime.now(timezone.utc)
    start_label = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*W}")
    print(f"  MACRO CONTEXT BACKTEST (SPY/QQQ) — {start_label} Lima")
    print(f"  Tickers ({len(TICKERS)}): {', '.join(TICKERS)}")
    print(f"  Configuraciones: BASELINE + {len(MACRO_CONFIGS)} filtros macro")
    print(f"  Timeframes: 5m (6 sem, macro 15m)  |  15m (12 sem, macro 1h)")
    print(f"  Logica: si mercado BEARISH -> LONG necesita +adj puntos extra (y viceversa)")
    print(f"{'='*W}")

    conclusions = {}

    for tf_key, cfg in TF_CONFIG.items():
        print(f"\n{'-'*W}")
        print(f"  [{tf_key.upper()}]  {cfg['label']}")
        print(f"{'-'*W}")

        start_dt    = (now_utc - timedelta(weeks=cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start   = (now_utc - timedelta(weeks=cfg["weeks"] + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        macro_start = (now_utc - timedelta(weeks=cfg["weeks"] + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # -- Fetch tickers ------------------------------------------------------
        ticker_data = {}
        print("  Cargando tickers:")
        for ticker in TICKERS:
            sys.stdout.write(f"    {ticker}... ")
            sys.stdout.flush()
            bars     = fetch_all(ticker, cfg["tf"],  start_dt)
            htf_bars = fetch_all(ticker, cfg["htf"], htf_start)
            if len(bars) >= cfg["warmup"] + 5:
                ticker_data[ticker] = (bars, htf_bars)
                print(f"{len(bars)} barras")
            else:
                print(f"skip ({len(bars)} barras)")

        if not ticker_data:
            print(f"  Sin datos — skip [{tf_key}]")
            continue

        # -- Fetch SPY / QQQ para macro -----------------------------------------
        print(f"\n  Cargando macro ({cfg['macro_tf']}):")
        sys.stdout.write(f"    SPY... "); sys.stdout.flush()
        spy_bars = fetch_all("SPY", cfg["macro_tf"], macro_start)
        print(f"{len(spy_bars)} barras")
        sys.stdout.write(f"    QQQ... "); sys.stdout.flush()
        qqq_bars = fetch_all("QQQ", cfg["macro_tf"], macro_start)
        print(f"{len(qqq_bars)} barras")

        sys.stdout.write(f"\n  Construyendo indice macro... ")
        sys.stdout.flush()
        macro_idx = build_macro_index(spy_bars, qqq_bars)
        print(f"{len(macro_idx)} puntos")

        # Distribucion de estados macro
        dist = macro_distribution(macro_idx)
        print(f"  Distribucion SPY: BULLISH {dist['BULLISH'][0]}bars({dist['BULLISH'][1]}%)  "
              f"NEUTRAL {dist['NEUTRAL'][0]}bars({dist['NEUTRAL'][1]}%)  "
              f"BEARISH {dist['BEARISH'][0]}bars({dist['BEARISH'][1]}%)")

        # -- Baseline ----------------------------------------------------------
        sys.stdout.write(f"\n  BASELINE... ")
        sys.stdout.flush()
        base_trades = []
        base_filtered = 0
        for ticker, (bars, htf_bars) in ticker_data.items():
            t, f = simulate(bars, htf_bars, cfg["warmup"], cfg["max_scans"], cfg["adverse"])
            base_trades += t
        base_s = stats(base_trades)
        print(f"OK  {base_s['n']} trades")

        # -- All macro configs -------------------------------------------------
        config_results = []
        for label, source, adj in MACRO_CONFIGS:
            sys.stdout.write(f"  {label:<8}... ")
            sys.stdout.flush()
            all_trades = []
            total_filtered = 0
            for ticker, (bars, htf_bars) in ticker_data.items():
                t, f = simulate(bars, htf_bars, cfg["warmup"], cfg["max_scans"], cfg["adverse"],
                                macro_idx, source, adj)
                all_trades     += t
                total_filtered += f
            s = stats(all_trades)
            config_results.append((label, source, adj, s, total_filtered))
            print(f"OK  {s['n']} trades (filtradas {total_filtered} entradas)")

        # -- Results table -----------------------------------------------------
        print(f"\n  RESULTADOS [{tf_key.upper()}]:")
        hdr = (f"  {'Config':<8}  {'N':>5}  {'WR':>4}  {'PnL':>9}  "
               f"{'Avg':>7}  {'PF':>5}  {'Mejor':>7}  {'Peor':>7}  "
               f"{'dPnL':>8}  {'dPF':>6}  {'dWR':>5}  {'Filtradas':>9}")
        print(hdr)
        print(f"  {'-'*90}")

        # Baseline row
        print(f"  {'BASE':<8}  {base_s['n']:>5}  {base_s['pct_win']:>3}%  "
              f"{fmt(base_s['total']):>9}%  {fmt(base_s['avg'],3):>7}%  "
              f"{base_s['pf']:>5.2f}  {fmt(base_s['best']):>7}%  "
              f"{fmt(base_s['worst']):>7}%  {'--':>8}  {'--':>6}  {'--':>5}  {'--':>9}")

        improvers = []
        degraders = []
        for label, source, adj, s, filtered in config_results:
            d_pnl = s["total"]   - base_s["total"]
            d_pf  = s["pf"]      - base_s["pf"]
            d_wr  = s["pct_win"] - base_s["pct_win"]
            is_better = d_pf > 0.05 and d_pnl > 0
            flag = " <<" if is_better and d_pf == max(
                x[3]["pf"] - base_s["pf"] for x in config_results) else ""
            print(f"  {label:<8}  {s['n']:>5}  {s['pct_win']:>3}%  "
                  f"{fmt(s['total']):>9}%  {fmt(s['avg'],3):>7}%  "
                  f"{s['pf']:>5.2f}  {fmt(s['best']):>7}%  "
                  f"{fmt(s['worst']):>7}%  "
                  f"{fmt(d_pnl):>8}pp  {fmt(d_pf):>6}  {fmt(d_wr,0):>5}pp  "
                  f"{filtered:>9}{flag}")
            if is_better:
                improvers.append((label, s, d_pnl, d_pf, d_wr, filtered))
            else:
                degraders.append((label, s))

        # -- By-direction analysis ----------------------------------------------
        print(f"\n  ANALISIS DE ENTRADAS FILTRADAS [{tf_key.upper()}]:")
        print(f"  (cuantas entradas bloqueo cada config y si fueron buenas o malas)")
        print(f"  {'Config':<8}  {'Filtradas':>9}  {'% del total':>11}  "
              f"{'N resultante':>12}  {'PF resultante':>13}  {'Veredicto':>20}")
        print(f"  {'-'*75}")
        for label, source, adj, s, filtered in config_results:
            pct_filt  = round(filtered / max(base_s["n"], 1) * 100)
            d_pf      = s["pf"] - base_s["pf"]
            if d_pf > 0.1:
                veredicto = "MEJORA (filtra trades malos)"
            elif d_pf > -0.1:
                veredicto = "NEUTRAL (sin efecto real)"
            else:
                veredicto = "PERJUDICA (filtra trades buenos)"
            print(f"  {label:<8}  {filtered:>9}  {pct_filt:>10}%  "
                  f"{s['n']:>12}  {s['pf']:>13.2f}  {veredicto:>20}")

        # -- Summary for this TF -----------------------------------------------
        print(f"\n  CONCLUSION [{tf_key.upper()}]:")
        print(f"    Configs que MEJORAN (dPF>0.05 y dPnL>0): {len(improvers)}/{len(MACRO_CONFIGS)}")
        print(f"    Configs que PERJUDICAN o NEUTRAL:          {len(degraders)}/{len(MACRO_CONFIGS)}")

        if improvers:
            best_cfg = max(improvers, key=lambda x: x[3] + x[2] / 100)  # max dPF + dPnL
            bl, bs, d_pnl, d_pf, d_wr, filtered = best_cfg
            print(f"\n    >> MEJOR CONFIG: {bl}")
            print(f"       PF     : {bs['pf']:.2f} vs BASE {base_s['pf']:.2f} ({fmt(d_pf)})")
            print(f"       WR     : {bs['pct_win']}% vs BASE {base_s['pct_win']}% ({fmt(d_wr,0)}pp)")
            print(f"       PnL    : {fmt(bs['total'])}% vs BASE {fmt(base_s['total'])}% ({fmt(d_pnl)}pp)")
            print(f"       Trades : {bs['n']} vs BASE {base_s['n']} ({filtered} filtradas)")
            conclusions[tf_key] = ("IMPLEMENTAR", bl, bs, base_s, d_pf, d_pnl, d_wr)
        else:
            print(f"\n    >> CONCLUSION: ninguna config macro mejora el sistema en {tf_key}")
            print(f"       El filtro macro filtra trades buenos y malos por igual,")
            print(f"       reduciendo oportunidades sin mejorar la precision.")
            print(f"       Recomendacion: NO implementar para {tf_key}.")
            conclusions[tf_key] = ("NO_IMPLEMENTAR", None, None, base_s, 0, 0, 0)

    # -- Global final summary -----------------------------------------------------
    print(f"\n{'='*W}")
    print(f"  CONCLUSION FINAL — MACRO CONTEXT FILTER")
    print(f"{'='*W}")
    for tf_key in TF_CONFIG:
        if tf_key not in conclusions:
            continue
        verdict, best_label, bs, base_s, d_pf, d_pnl, d_wr = conclusions[tf_key]
        if verdict == "IMPLEMENTAR":
            print(f"\n  [{tf_key.upper()}] RECOMENDADO: implementar {best_label}")
            print(f"         PF  : {base_s['pf']:.2f} -> {bs['pf']:.2f}  ({fmt(d_pf)})")
            print(f"         WR  : {base_s['pct_win']}% -> {bs['pct_win']}%  ({fmt(d_wr,0)}pp)")
            print(f"         PnL : {fmt(base_s['total'])}% -> {fmt(bs['total'])}%  ({fmt(d_pnl)}pp)")
        else:
            print(f"\n  [{tf_key.upper()}] NO IMPLEMENTAR — macro filter no beneficia el sistema")
            print(f"         Baseline PF={base_s['pf']:.2f}  WR={base_s['pct_win']}%  "
                  f"PnL={fmt(base_s['total'])}%  (mantener sin cambios)")
    print()
    print(f"  NOTA: Si ambos TF rechazan el filtro, la conclusion es clara: el sistema")
    print(f"  actual (HTF por ticker) ya captura suficiente contexto de tendencia.")
    print(f"  Si solo un TF mejora, implementar de forma condicional.")
    print()


if __name__ == "__main__":
    run()
