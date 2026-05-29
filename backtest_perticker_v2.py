#!/usr/bin/env python3
"""
Per-Ticker Parameter Optimizer v2
==================================
CLI: python backtest_perticker_v2.py NVDA

Optimiza params para UN ticker en dos fases:

  FASE 1 — Grid search (648 combos x TF):
    threshold_adj, n_binary_min, pattern_cap, confirm_req,
    max_scans, adverse_pct, block_short
    → mask = "all" (todos los criterios), scheme = "default"

  FASE 2 — Refinement (35 combos x TF):
    Con los mejores params de Fase 1, prueba:
    7 criteria_masks  x  5 pattern_schemes
    (nb se libera para adaptarse a la mascara)
    → decide si hay un filtro + esquema de pesos mejor

Filtros aplicados en simulacion:
  1. Entry params del grid (threshold_adj, n_binary_min, pattern_cap)
  2. criteria_mask (que indicadores binarios usar)
  3. pattern_scheme (como pesar los patrones de vela)
  4. Session threshold (hora Lima de cada barra — 100% reproducible)
  5. QQQ macro filter (solo 5m)
  6. Direction lock
  7. Time-stop (max_scans, adverse_pct)

Output: data/{TICKER}_config.json -> subir a Frontend: Watchlist -> Cargar Config
"""
import os, sys, json, time as _time, bisect, argparse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS
from utils.segment_config import (
    TIMESTOP_SCANS, TIMESTOP_ADVERSE_PCT,
    TICKER_THRESHOLDS, get_session_threshold_for_hour,
    TICKER_SEGMENT, get_segment_params,
)

ALPACA_BASE     = "https://data.alpaca.markets"
LIMA_OFFSET     = -5
QQQ_MACRO_ADJ   = 2
MIN_TRADES_BY_TF = {"1m": 20, "5m": 12, "15m": 8}  # trades minimos por TF
BLOCK_PF_THRESH  = 0.7

# ── Comisiones eToro (x1, sin apalancamiento) ─────────────────────────────────
# LONG:  $1.00 abrir + $1.00 cerrar = $2.00 fijos por trade (sin importar monto)
# SHORT: 0.15% abrir + 0.15% cerrar = 0.30% del total por trade
ETORO_LONG_FEE_USD  = 2.0
ETORO_SHORT_FEE_PCT = 0.30  # en % (no decimal)
ETORO_POSITION_SIZES = (300, 500, 1000, 2000, 5000)

TF_BASE = {
    "1m":  {"tf":"1Min",  "htf":"5Min",  "macro_tf":None,    "weeks":2,  "warmup":30,
            "max_scans_grid":[10,15,20], "adverse_grid":[0.3,0.5,0.7]},
    "5m":  {"tf":"5Min",  "htf":"15Min", "macro_tf":"15Min", "weeks":6,  "warmup":30,
            "max_scans_grid":[8,12,16],  "adverse_grid":[0.0,0.3,0.5]},
    "15m": {"tf":"15Min", "htf":"1Hour", "macro_tf":None,    "weeks":12, "warmup":30,
            "max_scans_grid":[3,4,6],    "adverse_grid":[0.5,1.0,1.5]},
}

# ── FASE 1: Grid de parametros de entrada ─────────────────────────────────────
# block_short ahora es parte del grid — el optimizer decide si bloquearlo
# c3_vol_min incluido en Fase1 con 2 valores: 0.0 (original) vs 1.2 (validado DNA)
GRID = {
    "threshold_adj": [0, 1, 2],
    "n_binary_min":  [1, 2, 3],
    "pattern_cap":   [2, 4],
    "confirm_req":   [2, 3],
    "block_short":   [False, True],
    "c3_vol_min":    [0.0, 1.2],   # 0=original, 1.2=MACD requiere vol>=1.2x (DNA validated)
}
# 3x3x2x2x2x2 x 3x3 (ms x adv) = 1,296 combos por TF

# ── FASE 2 nuevos grids (C3+VWAP) ─────────────────────────────────────────────
# c3_vol_min extendido: explora valores mas finos en Fase 2
C3_VOL_MIN_GRID = [0.0, 1.0, 1.2, 1.5]

# VWAP: (use_vwap, vwap_tolerance)
# False = sin VWAP (original), True = con VWAP y distintas tolerancias
VWAP_GRID = [
    (False, 0.0),     # sin VWAP (baseline)
    (True,  0.0),     # VWAP exacto: precio >= VWAP para LONG
    (True,  0.005),   # VWAP con 0.5% de tolerancia hacia abajo
    (True,  0.01),    # VWAP con 1.0% de tolerancia hacia abajo
]

# ── FASE 2A: Criteria masks (Nivel B) ─────────────────────────────────────────
# 7 subconjuntos curados de los 5 criterios binarios
# "all" es el baseline. Los demas prueban excluir indicadores problematicos.
CRITERIA_MASKS = [
    {"name":"all",       "bb":True,  "rsi":True,  "macd":True, "sr":True,  "vol":True},
    {"name":"no_rsi",    "bb":True,  "rsi":False, "macd":True, "sr":True,  "vol":True},
    {"name":"no_macd",   "bb":True,  "rsi":True,  "macd":False,"sr":True,  "vol":True},
    {"name":"no_sr",     "bb":True,  "rsi":True,  "macd":True, "sr":False, "vol":True},
    {"name":"no_vol",    "bb":True,  "rsi":True,  "macd":True, "sr":True,  "vol":False},
    {"name":"bb_rsi_vol","bb":True,  "rsi":True,  "macd":False,"sr":False, "vol":True},
    {"name":"core",      "bb":True,  "rsi":True,  "macd":True, "sr":False, "vol":False},
]
ALL_MASK = CRITERIA_MASKS[0]  # "all" — baseline de Fase 1

# ── FASE 2B: Pattern schemes (Nivel C) ───────────────────────────────────────
# Pesos originales de signal_engine.py: Engulfing/Morning Star=3, Hammer/SS=2, Harami=1
PATTERN_WEIGHTS_BASE = {
    "Bullish Engulfing":3,"Morning Star":3,"Three White Soldiers":2,
    "Hammer":2,"Piercing Line":2,"Dragonfly Doji":2,
    "Bullish Harami":1,"Inverted Hammer":1,
    "Bearish Engulfing":3,"Evening Star":3,"Three Black Crows":2,
    "Shooting Star":2,"Dark Cloud Cover":2,"Gravestone Doji":2,
    "Bearish Harami":1,"Hanging Man":1,
}

PATTERN_SCHEMES = [
    {"name":"default",     "map":{3:3, 2:2, 1:1}},  # pesos originales
    {"name":"disabled",    "map":{3:0, 2:0, 1:0}},  # patrones sin efecto
    {"name":"flat",        "map":{3:1, 2:1, 1:1}},  # todos igual peso 1
    {"name":"strong_only", "map":{3:3, 2:0, 1:0}},  # solo Engulfing/Morning Star
    {"name":"conservative","map":{3:2, 2:1, 1:0}},  # reduce pesos, ignora debiles
]
DEFAULT_SCHEME = PATTERN_SCHEMES[0]


# ── API ───────────────────────────────────────────────────────────────────────

def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
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
            if not data.get("next_page_token") or len(bars) >= limit:
                break
            params["page_token"] = data["next_page_token"]
            _time.sleep(0.12)
    except Exception as e:
        print(f"  [fetch_all] {ticker} {tf}: {e}")
    return [{"t":b["t"],"o":float(b["o"]),"h":float(b["h"]),
             "l":float(b["l"]),"c":float(b["c"]),"v":int(b["v"])} for b in bars]


# ── Helpers de tiempo ─────────────────────────────────────────────────────────

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")

def lima_hour(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).hour

def is_regular_session(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z","+00:00"))
    et = dt + timedelta(hours=-4)
    dm = et.hour * 60 + et.minute
    return 9*60+30 <= dm < 16*60


# ── QQQ macro index ───────────────────────────────────────────────────────────

def build_macro_index(qqq_bars):
    index = []
    for i in range(26, len(qqq_bars)):
        window = qqq_bars[max(0, i-49):i+1]
        trend  = compute_htf_trend(window)
        index.append((qqq_bars[i]["t"], trend))
    return index

def get_macro_trend_at(index, cur_t):
    if not index: return "NEUTRAL"
    ts_list = [x[0] for x in index]
    idx = bisect.bisect_right(ts_list, cur_t) - 1
    return index[idx][1] if idx >= 0 else "NEUTRAL"


# ── Score con mascara + esquema ───────────────────────────────────────────────

def _compute_masked_score(result, is_long, nb, pc, mask, scheme_map):
    """
    Re-calcula el puntaje de entrada aplicando criteria_mask y pattern_scheme.
    L3/S3 ya vienen condicionados por c3_vol_min desde compute_signal().
    L7/S7 (VWAP) se incluyen directamente — controlados por use_vwap en signal_params.
    Retorna (score, n_bin_activos).
    """
    crit = result.get("criteria", {})

    if is_long:
        n_bin = sum([
            bool(crit.get("L1")) and mask.get("bb",   True),
            bool(crit.get("L2")) and mask.get("rsi",  True),
            bool(crit.get("L3")) and mask.get("macd", True),
            bool(crit.get("L4")) and mask.get("sr",   True),
            bool(crit.get("L6")) and mask.get("vol",  True),
            bool(crit.get("L7")),  # VWAP: sin mask extra, use_vwap lo controla
        ])
        patterns = crit.get("bull_patterns", [])
    else:
        n_bin = sum([
            bool(crit.get("S1")) and mask.get("bb",   True),
            bool(crit.get("S2")) and mask.get("rsi",  True),
            bool(crit.get("S3")) and mask.get("macd", True),
            bool(crit.get("S4")) and mask.get("sr",   True),
            bool(crit.get("S6")) and mask.get("vol",  True),
            bool(crit.get("S7")),  # VWAP
        ])
        patterns = crit.get("bear_patterns", [])

    # Re-puntuar patrones con el esquema
    raw_pat = min(4, sum(
        scheme_map.get(PATTERN_WEIGHTS_BASE.get(p, 1), 0) for p in patterns
    ))
    pat_score = min(raw_pat, pc)
    return n_bin + pat_score, n_bin


def _apply_entry(result, pos_state, th, nb, pc, block_short, mask, scheme_map):
    """
    Evalua la decision de entrada con los params del grid + mask + scheme.
    Retorna (signal_final, result_con_scores_actualizados).
    El result actualizado propaga los scores modificados a filtros posteriores.
    """
    if pos_state != "SIN_POSICION":
        return result["signal"], result

    sig = result.get("signal", "ESPERAR")
    if sig not in ("ENTRAR_LONG", "ENTRAR_SHORT"):
        return sig, result

    if block_short and sig == "ENTRAR_SHORT":
        return "ESPERAR", result

    is_long = (sig == "ENTRAR_LONG")
    htf     = result.get("htf_trend", "NEUTRAL")

    score_l, nb_l = _compute_masked_score(result, True,  nb, pc, mask, scheme_map)
    score_s, nb_s = _compute_masked_score(result, False, nb, pc, mask, scheme_map)
    score   = score_l  if is_long else score_s
    n_bin   = nb_l     if is_long else nb_s

    # Umbral segun HTF
    if htf == "BULLISH":   thresh = max(3, 4+th) if is_long else min(9, 8+th)
    elif htf == "BEARISH": thresh = min(9, 8+th) if is_long else max(3, 4+th)
    else:                  thresh = max(3, 5+th)

    final_sig = sig if (score >= thresh and n_bin >= nb) else "ESPERAR"

    # Propagar scores actualizados para que session_threshold y QQQ los usen
    mod_result = {**result, "score_long": score_l, "score_short": score_s}
    return final_sig, mod_result


def _apply_qqq_macro(result, qqq_trend, adj=2):
    if qqq_trend == "NEUTRAL": return result["signal"]
    sl, ss = result["score_long"], result["score_short"]
    htf    = result.get("htf_trend", "NEUTRAL")
    if htf == "BULLISH":   lt, st = 4, 8
    elif htf == "BEARISH": lt, st = 8, 4
    else:                  lt, st = 5, 5
    if qqq_trend == "BEARISH":   lt += adj
    elif qqq_trend == "BULLISH": st += adj
    if sl >= lt: return "ENTRAR_LONG"
    if ss >= st: return "ENTRAR_SHORT"
    return "ESPERAR"


# ── Signal cache (speedup ~100x) ──────────────────────────────────────────────

def _sp_key(c3vm, use_v, vwap_tol):
    return (float(c3vm), bool(use_v), round(float(vwap_tol), 4))


def _derive_signal_for_pos(result, pos_state):
    """
    Re-derive result["signal"] for the actual pos_state using pre-computed
    scores (compute_signal was cached with SIN_POSICION).
    All indicator fields stay intact; only "signal" is overridden.
    """
    sl  = result["score_long"]
    ss  = result["score_short"]
    rsi = result["indicators"]["rsi"]
    htf = result["htf_trend"]

    if pos_state == "SIN_POSICION":
        if htf == "BULLISH":   lt, st = 4, 8
        elif htf == "BEARISH": lt, st = 8, 4
        else:                   lt, st = 5, 5
        c    = result.get("criteria", {})
        nb_l = sum(bool(c.get(k)) for k in ("L1","L2","L3","L4","L6","L7"))
        nb_s = sum(bool(c.get(k)) for k in ("S1","S2","S3","S4","S6","S7"))
        if   sl >= lt and nb_l >= 2: sig = "ENTRAR_LONG"
        elif ss >= st and nb_s >= 2: sig = "ENTRAR_SHORT"
        else:                        sig = "ESPERAR"
    elif pos_state == "LONG":
        if   rsi > 70: sig = "CERRAR_LONG"
        elif ss >= 3:  sig = "CERRAR_LONG"
        elif ss >= 2:  sig = "PRECAUCION"
        else:          sig = "MANTENER"
    elif pos_state == "SHORT":
        if   rsi < 30: sig = "CERRAR_SHORT"
        elif sl >= 3:  sig = "CERRAR_SHORT"
        elif sl >= 2:  sig = "PRECAUCION"
        else:          sig = "MANTENER"
    else:
        sig = "ESPERAR"

    return {**result, "signal": sig}


def build_bar_cache(bars, htf_bars, warmup, unique_params):
    """
    Pre-computes compute_signal() for every bar × each unique signal_params.
    unique_params: dict  _sp_key → signal_params dict.
    Returns list (index = bar i) with None for i < warmup and
    {"htf_trend": str, "by_key": {sp_key: result}} for i >= warmup.
    """
    cache = [None] * len(bars)
    htf_ptr = 0
    for i in range(warmup, len(bars)):
        cur_ts  = bars[i]["t"]
        candles = bars[max(0, i-119):i+1]
        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0, htf_ptr-50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"
        by_key = {key: compute_signal(candles, "SIN_POSICION", None, htf_trend, sp)
                  for key, sp in unique_params.items()}
        cache[i] = {"htf_trend": htf_trend, "by_key": by_key}
    return cache


# ── Simulacion completa ───────────────────────────────────────────────────────

def simulate(bars, htf_bars, ticker, interval, warmup,
             th, nb, pc, cr, ms, adv, block_short,
             mask=None, scheme_map=None,
             c3_vol_min=1.2, use_vwap=False, vwap_tolerance=0.0,
             qqq_macro_index=None, bar_cache=None):
    """
    Simulacion barra a barra con TODOS los filtros del watcher live.
    mask y scheme_map pueden ser None → usa all-criteria + default-weights.
    bar_cache: pre-computed cache from build_bar_cache() for ~100x speedup.
    """
    eff_mask   = mask       if mask       is not None else ALL_MASK
    eff_scheme = scheme_map if scheme_map is not None else DEFAULT_SCHEME["map"]
    eff_signal_params = {"c3_vol_min": c3_vol_min, "use_vwap": use_vwap,
                          "vwap_tolerance": vwap_tolerance}
    _cache_key = _sp_key(c3_vol_min, use_vwap, vwap_tolerance)

    cand_sig = None; cand_count = 0; cur_sig = "ESPERAR"
    lock_rem = 0; lock_dir = None; position = None; trades = []; htf_ptr = 0

    for i in range(warmup, len(bars)):
        bar       = bars[i]
        cur_ts    = bar["t"]
        cur_price = bar["c"]
        lh        = lima_hour(bar)
        is_reg    = is_regular_session(bar)

        pos_state = "SIN_POSICION" if not position else position["side"]

        # Time-stop
        if position and ms < 9999:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep_pos  = position["entry_price"]
            adverse = ((ep_pos-cur_price)/ep_pos*100 if pos_state=="LONG"
                       else (cur_price-ep_pos)/ep_pos*100)
            if position["scans_held"] >= ms and adverse >= adv:
                no  = bars[i+1]["o"] if i+1 < len(bars) else cur_price
                nt  = lima_ts(bars[i+1]) if i+1 < len(bars) else lima_ts(bar)
                pnl = ((no-ep_pos)/ep_pos*100 if pos_state=="LONG"
                       else (ep_pos-no)/ep_pos*100)
                trades.append({"side":pos_state,"in_t":position["entry_time"],"in_p":ep_pos,
                               "out_t":nt,"out_p":no,"pnl":round(pnl,3),"note":"TS"})
                position=None; lock_dir=None; lock_rem=0
                cur_sig="ESPERAR"; cand_sig=None; cand_count=0
                continue

        # Signal computation — use cache when available (100x faster)
        if bar_cache is not None and bar_cache[i] is not None:
            cached_bar = bar_cache[i]
            base = cached_bar["by_key"].get(_cache_key)
            if base is not None:
                result = _derive_signal_for_pos(base, pos_state)
            else:
                candles   = bars[max(0, i-119):i+1]
                result    = compute_signal(candles, pos_state, None,
                                           cached_bar["htf_trend"], eff_signal_params)
        else:
            candles   = bars[max(0, i-119):i+1]
            while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
                htf_ptr += 1
            htf_slice = htf_bars[max(0, htf_ptr-50):htf_ptr]
            htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"
            result    = compute_signal(candles, pos_state, None, htf_trend, eff_signal_params)

        # ── Filter 1+2+3: Entry decision con mask + scheme ───────────────────
        raw_sig, mod_result = _apply_entry(
            result, pos_state, th, nb, pc, block_short, eff_mask, eff_scheme)

        # ── Filter 4: Session threshold (usa scores modificados) ─────────────
        if raw_sig in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            sess_thresh = get_session_threshold_for_hour(lh, is_reg, interval)
            sc = mod_result["score_long"] if raw_sig=="ENTRAR_LONG" else mod_result["score_short"]
            if sc < sess_thresh:
                raw_sig = "ESPERAR"

        # ── Filter 5: Ticker threshold ────────────────────────────────────────
        if raw_sig in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            tk_min = TICKER_THRESHOLDS.get(ticker)
            if tk_min:
                sc = mod_result["score_long"] if raw_sig=="ENTRAR_LONG" else mod_result["score_short"]
                if sc < tk_min: raw_sig = "ESPERAR"

        # ── Filter 6: QQQ macro 5m (usa scores modificados) ──────────────────
        if interval == "5m" and raw_sig in ("ENTRAR_LONG","ENTRAR_SHORT") and qqq_macro_index:
            qt = get_macro_trend_at(qqq_macro_index, cur_ts)
            if qt != "NEUTRAL":
                raw_sig = _apply_qqq_macro(mod_result, qt, QQQ_MACRO_ADJ)

        # ── Filter 7: Direction lock ──────────────────────────────────────────
        if lock_rem > 0 and lock_dir:
            if (lock_dir=="SHORT" and raw_sig=="ENTRAR_SHORT") or \
               (lock_dir=="LONG"  and raw_sig=="ENTRAR_LONG"):
                raw_sig = "ESPERAR"
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0: lock_dir = None

        # Confirmacion (cr scans consecutivos)
        confirmed = False
        if raw_sig == cand_sig:
            cand_count += 1
            if cand_count >= cr and raw_sig != cur_sig:
                confirmed = True; cur_sig = raw_sig
            if cand_count >= cr:
                cand_sig = None; cand_count = 0
        else:
            cand_sig = raw_sig; cand_count = 1

        if not confirmed: continue

        if cur_sig == "ENTRAR_LONG":    lock_dir="SHORT"; lock_rem=DIRECTION_LOCK_SCANS
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
            if side == "SHORT": pnl = -pnl
            trades.append({"side":side,"in_t":p["entry_time"],"in_p":p["entry_price"],
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
        lp = bars[-1]["c"]; lt2 = lima_ts(bars[-1])+"*"; side = position["side"]
        pnl = (lp-position["entry_price"])/position["entry_price"]*100
        if side == "SHORT": pnl = -pnl
        trades.append({"side":side,"in_t":position["entry_time"],"in_p":position["entry_price"],
                       "out_t":lt2,"out_p":lp,"pnl":round(pnl,3),"note":"fp"})
    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats(trades):
    if not trades:
        return {"n":0,"pct_win":0.0,"total":0.0,"avg":0.0,"pf":0.0}
    pnls  = [t["pnl"] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p <= 0]
    gw    = sum(wins); gl = abs(sum(losses))
    pf    = round(gw/gl,2) if gl > 0 else (99.0 if gw > 0 else 0.0)
    return {"n":len(trades),"pct_win":round(len(wins)/len(trades)*100),
            "total":round(sum(pnls),2),"avg":round(sum(pnls)/len(trades),3),"pf":pf}


def hourly_stats(trades):
    """
    Agrupa los trades por hora Lima de entrada y calcula WR/PF/avg por hora.
    Usa el campo in_t que ya esta en formato "HH:MM" Lima.
    Marca horas con n < MIN_HOURLY_SAMPLE como insuficientes.
    """
    MIN_HOURLY_SAMPLE = 5
    by_hour = {}
    for t in trades:
        try:
            h = int(t["in_t"].split(":")[0])
        except Exception:
            continue
        by_hour.setdefault(h, []).append(t["pnl"])

    result = {}
    for h in sorted(by_hour.keys()):
        pnls   = by_hour[h]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gw     = sum(wins); gl = abs(sum(losses))
        pf     = round(gw/gl,2) if gl > 0 else (99.0 if gw > 0 else 0.0)
        result[str(h)] = {
            "n":       len(pnls),
            "wr_pct":  round(len(wins)/len(pnls)*100),
            "pf":      pf,
            "avg_pnl": round(sum(pnls)/len(pnls), 3),
            "ok":      len(pnls) >= MIN_HOURLY_SAMPLE,
        }
    return result

def etoro_fee_analysis(trades):
    """
    Calcula estadisticas netas tras comisiones reales de eToro x1:
      LONG:  $2 fijos por round-trip → fee % depende del monto de posicion
      SHORT: 0.30% del monto por round-trip → fee fija en %

    Retorna dict con analisis por lado + recomendacion de posicion minima.
    Si SHORT no es viable (net_avg <= 0), lo marca como 'viable: False'.
    """
    longs  = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    result = {}

    # ── LONG ─────────────────────────────────────────────────────────────────
    if longs:
        gpnls    = [t["pnl"] for t in longs]
        g_avg    = sum(gpnls) / len(gpnls)
        g_wins   = [p for p in gpnls if p > 0]
        g_wr     = round(len(g_wins)/len(gpnls)*100)

        # Break-even: monto tal que fee_pct == gross_avg
        # fee_pct = 200/monto, break_even cuando 200/monto = gross_avg → monto = 200/gross_avg
        breakeven = round(200.0 / g_avg) if g_avg > 0 else None

        net_by_size = {}
        for sz in ETORO_POSITION_SIZES:
            fee_pct  = 200.0 / sz          # $2 expresado como % de la posicion
            npnls    = [p - fee_pct for p in gpnls]
            nwins    = [p for p in npnls if p > 0]
            n_avg    = round(sum(npnls)/len(npnls), 3)
            n_wr     = round(len(nwins)/len(npnls)*100)
            gw       = sum(p for p in npnls if p > 0)
            gl       = abs(sum(p for p in npnls if p <= 0))
            n_pf     = round(gw/gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0)
            n_usd    = round(n_avg / 100 * sz, 2)   # ganancia neta en dolares
            net_by_size[str(sz)] = {
                "net_avg_pnl": n_avg,
                "net_wr_pct":  n_wr,
                "net_pf":      n_pf,
                "net_usd":     n_usd,
                "viable":      n_avg > 0,
            }

        result["long"] = {
            "n":                       len(longs),
            "gross_avg_pnl":           round(g_avg, 3),
            "gross_wr_pct":            g_wr,
            "flat_fee_usd":            ETORO_LONG_FEE_USD,
            "breakeven_position_usd":  breakeven,
            "net_by_position_size":    net_by_size,
        }

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if shorts:
        gpnls  = [t["pnl"] for t in shorts]
        g_avg  = sum(gpnls) / len(gpnls)
        g_wins = [p for p in gpnls if p > 0]
        g_wr   = round(len(g_wins)/len(gpnls)*100)

        fee_pct = ETORO_SHORT_FEE_PCT
        npnls   = [p - fee_pct for p in gpnls]
        nwins   = [p for p in npnls if p > 0]
        n_avg   = round(sum(npnls)/len(npnls), 3)
        n_wr    = round(len(nwins)/len(npnls)*100)
        gw      = sum(p for p in npnls if p > 0)
        gl      = abs(sum(p for p in npnls if p <= 0))
        n_pf    = round(gw/gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0)
        viable  = n_avg > 0

        result["short"] = {
            "n":              len(shorts),
            "gross_avg_pnl":  round(g_avg, 3),
            "gross_wr_pct":   g_wr,
            "pct_fee":        fee_pct,
            "net_avg_pnl":    n_avg,
            "net_wr_pct":     n_wr,
            "net_pf":         n_pf,
            "viable":         viable,
        }

    return result


def composite_score(s, min_trades=20):
    if s["n"] < min_trades: return -999.0
    return round(0.5*min(s["pf"],6.0)/6.0*5 + 0.3*(s["pct_win"]/100)*5 +
                 0.2*min(max(s["avg"]*15,0),5), 4)

def fmt(v, d=2): return f"{'+' if v>=0 else ''}{v:.{d}f}"


# ── Runner ────────────────────────────────────────────────────────────────────

def run(ticker):
    W       = 96
    now_utc = datetime.now(timezone.utc)
    label   = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")
    seg     = TICKER_SEGMENT.get(ticker, "DEFAULT")
    n_p1    = (len(GRID["threshold_adj"]) * len(GRID["n_binary_min"]) *
               len(GRID["pattern_cap"])    * len(GRID["confirm_req"])  *
               len(GRID["block_short"])    * len(GRID["c3_vol_min"])   * 3 * 3)
    n_p2    = (len(CRITERIA_MASKS) * len(PATTERN_SCHEMES) *
               len(GRID["n_binary_min"]) * len(C3_VOL_MIN_GRID) * len(VWAP_GRID))

    print(f"\n{'='*W}")
    print(f"  PER-TICKER OPTIMIZER v2 — {ticker}  [{label} Lima]")
    print(f"  Segmento: {seg}  |  "
          f"Fase1: {n_p1} combos  |  "
          f"Fase2: {n_p2} combos  |  "
          f"MIN_TRADES: 1m={MIN_TRADES_BY_TF['1m']} 5m={MIN_TRADES_BY_TF['5m']} 15m={MIN_TRADES_BY_TF['15m']}")
    print(f"  Filtros: session-threshold + QQQ(5m) + dir-lock + criteria-mask + pattern-scheme")
    print(f"{'='*W}")

    # Pre-fetch barras
    ticker_data = {}
    print("\n  Pre-cargando barras...")
    for tf_key, cfg in TF_BASE.items():
        start_dt  = (now_utc - timedelta(weeks=cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start = (now_utc - timedelta(weeks=cfg["weeks"]+2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars      = fetch_all(ticker, cfg["tf"],  start_dt)
        htf_bars  = fetch_all(ticker, cfg["htf"], htf_start)
        if len(bars) >= cfg["warmup"]+5:
            ticker_data[tf_key] = (bars, htf_bars)
        print(f"    {tf_key}: {len(bars)}b barras  /  {len(htf_bars)}b HTF")

    qqq_macro_index = None
    if "5m" in ticker_data:
        qqq_start = (now_utc - timedelta(weeks=TF_BASE["5m"]["weeks"]+2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        qqq_bars  = fetch_all("QQQ", "15Min", qqq_start)
        qqq_macro_index = build_macro_index(qqq_bars) if len(qqq_bars) >= 30 else None
        print(f"    QQQ macro: {len(qqq_bars)}b barras  |  {len(qqq_macro_index or [])} pts")

    # Baseline con params del segmento
    print(f"\n  Baseline segmento [{seg}]:")
    baseline_by_tf = {}
    for tf_key in ["1m","5m","15m"]:
        if tf_key not in ticker_data: continue
        bars, htf_bars = ticker_data[tf_key]
        cfg   = TF_BASE[tf_key]
        seg_p = get_segment_params(ticker, tf_key)
        ms    = seg_p["max_scans"]   if seg_p["max_scans"]   is not None else cfg["max_scans_grid"][1]
        adv   = seg_p["adverse_pct"] if seg_p["adverse_pct"] is not None else cfg["adverse_grid"][1]
        base_t = simulate(bars, htf_bars, ticker, tf_key, cfg["warmup"],
                          seg_p["threshold_adj"], seg_p["n_binary_min"], seg_p["pattern_cap"],
                          seg_p["confirm_req"], ms, adv, seg_p.get("block_short", False),
                          qqq_macro_index=qqq_macro_index if tf_key=="5m" else None)
        base_s = stats(base_t)
        baseline_by_tf[tf_key] = base_s
        print(f"    {tf_key}: n={base_s['n']:>4}  WR={base_s['pct_win']:>3}%  "
              f"PF={base_s['pf']:>5.2f}  avg={fmt(base_s['avg'],3)}%")

    # ── FASE 1 + FASE 2 por TF ────────────────────────────────────────────────
    best_by_tf = {}

    for tf_key in ["1m","5m","15m"]:
        if tf_key not in ticker_data:
            print(f"\n  [{tf_key}] Sin datos — skip"); continue

        bars, htf_bars = ticker_data[tf_key]
        cfg            = TF_BASE[tf_key]
        base_s         = baseline_by_tf.get(tf_key, {})
        qqq_idx        = qqq_macro_index if tf_key=="5m" else None

        # ── FASE 1: Grid completo, mask=all, scheme=default ───────────────────
        phase1_results = []
        # Pre-build cache for Phase 1 unique signal_params (2 combos: c3vm 0.0 / 1.2)
        _p1_unique = {_sp_key(c3vm, False, 0.0): {"c3_vol_min": c3vm, "use_vwap": False, "vwap_tolerance": 0.0}
                      for c3vm in GRID["c3_vol_min"]}
        sys.stdout.write(f"\n  [{tf_key}] Fase 1 ({n_p1} combos) caching {len(_p1_unique)} signal_params... ")
        sys.stdout.flush()
        _p1_cache = build_bar_cache(bars, htf_bars, cfg["warmup"], _p1_unique)
        sys.stdout.write("grid... ")
        sys.stdout.flush()

        for th in GRID["threshold_adj"]:
          for nb in GRID["n_binary_min"]:
            for pc in GRID["pattern_cap"]:
              for cr in GRID["confirm_req"]:
                for bs in GRID["block_short"]:
                  for c3vm in GRID["c3_vol_min"]:
                    for ms in cfg["max_scans_grid"]:
                      for adv in cfg["adverse_grid"]:
                        t  = simulate(bars, htf_bars, ticker, tf_key, cfg["warmup"],
                                      th, nb, pc, cr, ms, adv, bs,
                                      mask=ALL_MASK, scheme_map=DEFAULT_SCHEME["map"],
                                      c3_vol_min=c3vm, use_vwap=False,
                                      qqq_macro_index=qqq_idx,
                                      bar_cache=_p1_cache)
                        s  = stats(t)
                        cs = composite_score(s, MIN_TRADES_BY_TF[tf_key])
                        phase1_results.append({"th":th,"nb":nb,"pc":pc,"cr":cr,
                                               "bs":bs,"ms":ms,"adv":adv,
                                               "c3vm":c3vm,
                                               "mask":"all","scheme":"default",
                                               "use_vwap":False,"vwap_tol":0.0,
                                               "stats":s,"score":cs})

        phase1_results.sort(key=lambda x: -x["score"])
        _mt = MIN_TRADES_BY_TF[tf_key]
        viable1 = [r for r in phase1_results if r["stats"]["n"] >= _mt]
        best1   = viable1[0] if viable1 else phase1_results[0]
        print(f"OK  n={best1['stats']['n']}  WR={best1['stats']['pct_win']}%  "
              f"PF={best1['stats']['pf']:.2f}  bs={best1['bs']}  c3vm={best1['c3vm']}")

        # ── FASE 2: Refinement — mask × scheme × nb × c3_vol_min × vwap ────────
        # Params estructurales (th, pc, cr, ms, adv, bs) fijados del mejor de Fase 1.
        # Exploramos: criterios mascarados, esquema de patrones, volumen MACD, VWAP.
        phase2_results = []
        p1_fixed = best1
        n_p2 = (len(CRITERIA_MASKS) * len(PATTERN_SCHEMES) *
                len(GRID["n_binary_min"]) * len(C3_VOL_MIN_GRID) * len(VWAP_GRID))
        # Pre-build cache for Phase 2 unique signal_params (4×4=16 combos)
        _p2_unique = {_sp_key(c3vm, use_v, vwap_tol): {"c3_vol_min": c3vm, "use_vwap": use_v, "vwap_tolerance": vwap_tol}
                      for c3vm in C3_VOL_MIN_GRID for (use_v, vwap_tol) in VWAP_GRID}
        sys.stdout.write(f"  [{tf_key}] Fase 2 ({n_p2} combos) caching {len(_p2_unique)} signal_params... ")
        sys.stdout.flush()
        _p2_cache = build_bar_cache(bars, htf_bars, cfg["warmup"], _p2_unique)
        sys.stdout.write("grid... ")
        sys.stdout.flush()
        for mask_cfg in CRITERIA_MASKS:
          for sch_cfg in PATTERN_SCHEMES:
            for nb in GRID["n_binary_min"]:
              for c3vm in C3_VOL_MIN_GRID:
                for (use_v, vwap_tol) in VWAP_GRID:
                    t  = simulate(bars, htf_bars, ticker, tf_key, cfg["warmup"],
                                  p1_fixed["th"], nb, p1_fixed["pc"],
                                  p1_fixed["cr"], p1_fixed["ms"], p1_fixed["adv"],
                                  p1_fixed["bs"],
                                  mask=mask_cfg, scheme_map=sch_cfg["map"],
                                  c3_vol_min=c3vm,
                                  use_vwap=use_v, vwap_tolerance=vwap_tol,
                                  qqq_macro_index=qqq_idx,
                                  bar_cache=_p2_cache)
                    s  = stats(t)
                    cs = composite_score(s, _mt)
                    phase2_results.append({
                        "th":p1_fixed["th"], "nb":nb, "pc":p1_fixed["pc"],
                        "cr":p1_fixed["cr"], "ms":p1_fixed["ms"], "adv":p1_fixed["adv"],
                        "bs":p1_fixed["bs"],
                        "c3vm":c3vm,
                        "use_vwap":use_v, "vwap_tol":vwap_tol,
                        "mask":mask_cfg["name"], "scheme":sch_cfg["name"],
                        "stats":s, "score":cs,
                        "_mask_cfg":mask_cfg, "_scheme_cfg":sch_cfg,
                    })

        phase2_results.sort(key=lambda x: -x["score"])
        viable2 = [r for r in phase2_results if r["stats"]["n"] >= _mt]
        best2   = viable2[0] if viable2 else None
        if best2:
            improved = best2["score"] > best1["score"]
            print(f"OK  n={best2['stats']['n']}  WR={best2['stats']['pct_win']}%  "
                  f"PF={best2['stats']['pf']:.2f}  "
                  f"mask={best2['mask']}  scheme={best2['scheme']}"
                  f"{'  *** MEJOR QUE FASE1' if improved else ''}")
        else:
            print(f"Sin combos con n>={_mt}")

        # Ganador: el mejor entre Fase 1 y Fase 2
        if best2 and best2["score"] > best1["score"]:
            best_final = best2
        else:
            best_final = {**best1,
                          "_mask_cfg":ALL_MASK,
                          "_scheme_cfg":DEFAULT_SCHEME}

        # Veredicto final
        n     = best_final["stats"]["n"]
        pf    = best_final["stats"]["pf"]
        viable_any = [r for r in (viable1 + (viable2 or [])) if r["stats"]["n"] >= _mt]
        best_viable_pf = max((r["stats"]["pf"] for r in viable_any), default=0.0)

        if not viable_any:
            verdict = "BLOCK"   # sin datos suficientes → no operar (antes: USE_SEGMENT)
        elif best_viable_pf < BLOCK_PF_THRESH:
            verdict = "BLOCK"
        else:
            verdict = "CUSTOM"

        # Mostrar Top 5 de Fase 1 + ganador de Fase 2
        if viable1:
            print(f"\n  [{tf_key}] FASE 1 — Top 5 (n>={_mt}):")
            print(f"  {'R':>3}  {'th':>3}  {'nb':>3}  {'pc':>3}  {'cr':>3}  {'bs':>5}  "
                  f"{'ms':>4}  {'adv':>5}  {'N':>5}  {'WR':>4}  {'PF':>5}  {'Score':>6}  vs_seg")
            print(f"  {'-'*88}")
            for rank, r in enumerate(viable1[:5], 1):
                s    = r["stats"]
                d_pf = s["pf"] - base_s.get("pf", 0)
                flag = " <<" if rank==1 else ""
                print(f"  {rank:>3}  {r['th']:>+3}  {r['nb']:>3}  {r['pc']:>3}  {r['cr']:>3}  "
                      f"{'True' if r['bs'] else 'False':>5}  "
                      f"{r['ms']:>4}  {r['adv']:>5.1f}  {s['n']:>5}  {s['pct_win']:>3}%  "
                      f"{s['pf']:>5.2f}  {r['score']:>6.3f}  dPF={fmt(d_pf)}{flag}")

        if viable2:
            print(f"\n  [{tf_key}] FASE 2 — Top 5 (n>={_mt}):")
            print(f"  {'R':>3}  {'mask':>12}  {'scheme':>12}  {'nb':>3}  "
                  f"{'c3vm':>5}  {'vwap':>9}  "
                  f"{'N':>5}  {'WR':>4}  {'PF':>5}  {'Score':>6}  vs_fase1")
            print(f"  {'-'*88}")
            for rank, r in enumerate(viable2[:5], 1):
                s    = r["stats"]
                d_pf = s["pf"] - best1["stats"].get("pf", 0)
                flag = " <<" if rank==1 else ""
                vwap_str = f"T/{r['vwap_tol']:.3f}" if r.get("use_vwap") else "False"
                print(f"  {rank:>3}  {r['mask']:>12}  {r['scheme']:>12}  {r['nb']:>3}  "
                      f"{r.get('c3vm',1.2):>5.1f}  {vwap_str:>9}  "
                      f"{s['n']:>5}  {s['pct_win']:>3}%  {s['pf']:>5.2f}  "
                      f"{r['score']:>6.3f}  dPF={fmt(d_pf)}{flag}")

        vwap_info = (f"vwap=T/{best_final.get('vwap_tol',0.0):.3f}"
                     if best_final.get("use_vwap") else "vwap=False")
        print(f"\n  [{tf_key}] VEREDICTO: {verdict}  "
              f"| mask={best_final.get('mask','all')}  "
              f"scheme={best_final.get('scheme','default')}  "
              f"c3vm={best_final.get('c3vm',1.2)}  {vwap_info}")

        best_by_tf[tf_key] = {
            "verdict":     verdict,
            "best_final":  best_final,
            "baseline_s":  base_s,
        }

    # ── Construir JSON de salida ───────────────────────────────────────────────
    tfs_out = {}
    for tf_key in ["1m","5m","15m"]:
        res = best_by_tf.get(tf_key)
        if not res: continue
        verdict = res["verdict"]
        bf      = res["best_final"]
        seg_p   = get_segment_params(ticker, tf_key)

        if verdict == "CUSTOM" and bf["stats"]["n"] >= MIN_TRADES_BY_TF[tf_key]:
            mask_cfg   = bf.get("_mask_cfg", ALL_MASK)
            scheme_cfg = bf.get("_scheme_cfg", DEFAULT_SCHEME)
            params = {
                "threshold_adj":  bf["th"],
                "n_binary_min":   bf["nb"],
                "pattern_cap":    bf["pc"],
                "confirm_req":    bf["cr"],
                "max_scans":      bf["ms"],
                "adverse_pct":    bf["adv"],
                "block_new":      False,
                "block_short":    bool(bf["bs"]),
                "criteria_mask":  mask_cfg,
                "pattern_scheme": scheme_cfg["name"],
                "c3_vol_min":     bf.get("c3vm", 1.2),
                "use_vwap":       bool(bf.get("use_vwap", False)),
                "vwap_tolerance": bf.get("vwap_tol", 0.0),
            }
            s = bf["stats"]
        elif verdict == "BLOCK":
            mask_cfg   = bf.get("_mask_cfg", ALL_MASK)
            scheme_cfg = bf.get("_scheme_cfg", DEFAULT_SCHEME)
            params = {
                "threshold_adj":  seg_p["threshold_adj"],
                "n_binary_min":   seg_p["n_binary_min"],
                "pattern_cap":    seg_p["pattern_cap"],
                "confirm_req":    seg_p["confirm_req"],
                "max_scans":      seg_p.get("max_scans"),
                "adverse_pct":    seg_p.get("adverse_pct"),
                "block_new":      True,
                "block_short":    bool(seg_p.get("block_short", False)),
                "criteria_mask":  mask_cfg,
                "pattern_scheme": scheme_cfg["name"],
                "c3_vol_min":     1.2,
                "use_vwap":       False,
                "vwap_tolerance": 0.0,
            }
            s = bf["stats"] if bf else {"pf":0.0,"pct_win":0,"avg":0.0,"n":0}
        else:  # USE_SEGMENT
            bs_s = res["baseline_s"]
            params = {
                "threshold_adj":  seg_p["threshold_adj"],
                "n_binary_min":   seg_p["n_binary_min"],
                "pattern_cap":    seg_p["pattern_cap"],
                "confirm_req":    seg_p["confirm_req"],
                "max_scans":      seg_p.get("max_scans"),
                "adverse_pct":    seg_p.get("adverse_pct"),
                "block_new":      False,
                "block_short":    bool(seg_p.get("block_short", False)),
                "criteria_mask":  ALL_MASK,
                "pattern_scheme": "default",
                "c3_vol_min":     1.2,
                "use_vwap":       False,
                "vwap_tolerance": 0.0,
            }
            s = {"pf":bs_s.get("pf",0.0),"pct_win":bs_s.get("pct_win",0),
                 "avg":bs_s.get("avg",0.0),"n":bs_s.get("n",0)}

        params["stats"] = {
            "pf":       s["pf"],
            "wr_pct":   float(s["pct_win"]),
            "avg_pnl":  s["avg"],
            "n_trades": s["n"],
        }
        tfs_out[tf_key] = params

    # ── Analisis comisiones + horario — re-corre con mejores params por TF ──
    print(f"\n{'='*W}")
    print(f"  ANALISIS COMISIONES eToro (x1) — {ticker}")
    print(f"  LONG: ${ETORO_LONG_FEE_USD:.0f} fijos por trade  |  "
          f"SHORT: {ETORO_SHORT_FEE_PCT}% por trade")

    hourly_by_tf  = {}
    fee_by_tf     = {}
    for tf_key in ["1m","5m","15m"]:
        res = best_by_tf.get(tf_key)
        if not res or tf_key not in ticker_data:
            continue
        bf         = res["best_final"]
        bars_h, htf_h = ticker_data[tf_key]
        cfg_h      = TF_BASE[tf_key]
        qqq_idx_h  = qqq_macro_index if tf_key=="5m" else None
        mask_h     = bf.get("_mask_cfg",   ALL_MASK)
        scheme_h   = bf.get("_scheme_cfg", DEFAULT_SCHEME)

        # Use the Phase 2 cache for this TF (already built above)
        _fee_sp   = _sp_key(bf.get("c3vm", 1.2), bf.get("use_vwap", False), bf.get("vwap_tol", 0.0))
        _fee_uniq = {_fee_sp: {"c3_vol_min": bf.get("c3vm", 1.2),
                                "use_vwap": bf.get("use_vwap", False),
                                "vwap_tolerance": bf.get("vwap_tol", 0.0)}}
        _fee_cache = build_bar_cache(bars_h, htf_h, cfg_h["warmup"], _fee_uniq)

        best_trades = simulate(
            bars_h, htf_h, ticker, tf_key, cfg_h["warmup"],
            bf["th"], bf["nb"], bf["pc"], bf["cr"],
            bf["ms"], bf["adv"], bf["bs"],
            mask=mask_h, scheme_map=scheme_h["map"],
            c3_vol_min=bf.get("c3vm",     1.2),
            use_vwap=  bf.get("use_vwap", False),
            vwap_tolerance=bf.get("vwap_tol", 0.0),
            qqq_macro_index=qqq_idx_h,
            bar_cache=_fee_cache,
        )

        # ── Fee analysis ─────────────────────────────────────────────────────
        fee_data = etoro_fee_analysis(best_trades)
        fee_by_tf[tf_key] = fee_data

        print(f"\n  [{tf_key}]  n_total={len(best_trades)}")

        if "long" in fee_data:
            ld  = fee_data["long"]
            be  = ld["breakeven_position_usd"]
            be_s = f"${be:,.0f}" if be else "N/A"
            print(f"  LONG  gross: n={ld['n']:>3} WR={ld['gross_wr_pct']}% avg={fmt(ld['gross_avg_pnl'],3)}%"
                  f"  |  Break-even: {be_s}")
            print(f"  {'Posicion':>8}  {'Fee%':>6}  {'Net avg':>8}  {'Net WR':>7}  {'Net PF':>6}  "
                  f"{'Neto $':>8}  Rentable")
            print(f"  {'-'*60}")
            for sz_s, nd in ld["net_by_position_size"].items():
                sz   = int(sz_s)
                fp   = round(200.0/sz, 3)
                mark = "  SI" if nd["viable"] else "  NO"
                viable_szs = [int(k) for k, v in ld["net_by_position_size"].items() if v["viable"]]
                best_mark  = "  <-- RECOMENDADO" if (nd["viable"] and viable_szs and
                              int(sz_s) == min(viable_szs)) else ""
                print(f"  ${sz:>7,}  {fp:>5.2f}%  {fmt(nd['net_avg_pnl'],3):>8}%  "
                      f"{nd['net_wr_pct']:>6}%  {nd['net_pf']:>6.2f}  "
                      f"${nd['net_usd']:>7.2f}{mark}{best_mark}")

        if "short" in fee_data:
            sd = fee_data["short"]
            viab = "VIABLE" if sd["viable"] else "NO RENTABLE con comisiones actuales"
            print(f"  SHORT gross: n={sd['n']:>3} WR={sd['gross_wr_pct']}% avg={fmt(sd['gross_avg_pnl'],3)}%"
                  f"  |  Net tras {sd['pct_fee']}%: {fmt(sd['net_avg_pnl'],3)}% -> {viab}")
            if not sd["viable"]:
                print(f"  >>> SHORT BLOQUEADO automaticamente en output (net_avg <= 0)")

        # ── Hourly analysis ───────────────────────────────────────────────────
        hourly = hourly_stats(best_trades)
        hourly_by_tf[tf_key] = hourly

        if not hourly:
            continue

        ok_hours = {h: d for h, d in hourly.items() if d["ok"]}
        best_h   = max(ok_hours, key=lambda h: ok_hours[h]["wr_pct"]) if ok_hours else None
        worst_h  = min(ok_hours, key=lambda h: ok_hours[h]["wr_pct"]) if ok_hours else None

        data_weeks = TF_BASE[tf_key]["weeks"]
        print(f"\n  ANALISIS HORARIO [{tf_key}] {data_weeks} sem  "
              f"{'— aumentar weeks para mas precision' if data_weeks <= 2 else ''}")
        print(f"  {'Hora':>6}  {'N':>4}  {'WR':>5}  {'PF':>5}  {'Avg%':>7}  {'Bar':>20}")
        print(f"  {'-'*58}")
        for h_str in sorted(hourly.keys(), key=int):
            hd  = hourly[h_str]
            wr  = hd["wr_pct"]
            ins = " (*)" if not hd["ok"] else "     "
            bar_len = max(0, round(wr / 5))
            bar_col = ("#" if wr >= 65 else ("+" if wr >= 50 else "-"))
            bar     = f"{bar_col * bar_len:<20}"
            tag = ""
            if h_str == best_h:  tag = "  << MEJOR"
            if h_str == worst_h: tag = "  << EVITAR"
            print(f"  {h_str:>4}h  {hd['n']:>4}  {wr:>4}%  "
                  f"{hd['pf']:>5.2f}  {fmt(hd['avg_pnl'],3):>7}%  {bar}{ins}{tag}")

        if ok_hours:
            top_hours = sorted(ok_hours, key=lambda h: (
                -ok_hours[h]["wr_pct"] * 0.5 - ok_hours[h]["pf"] * 0.3 -
                ok_hours[h]["avg_pnl"] * 15 * 0.2
            ))[:3]
            print(f"\n  Horas recomendadas: "
                  + ", ".join(f"{h}h ({ok_hours[h]['wr_pct']}% WR)" for h in top_hours))

    # ── Auto-override block_short si SHORT no es viable con comisiones ──────
    for tf_key, tf_params in tfs_out.items():
        fee_tf  = fee_by_tf.get(tf_key, {})
        sd      = fee_tf.get("short", {})
        # Si SHORT no es rentable neto y el optimizer no lo bloqueo, forzar
        if sd and not sd.get("viable", True) and not tf_params.get("block_short"):
            tf_params["block_short"] = True
            tf_params["block_short_reason"] = "auto: SHORT net_avg <= 0 tras comisiones eToro"

    # ── Construir output final ────────────────────────────────────────────────
    output = {
        "ticker":           ticker,
        "calibrated_at":    datetime.now(timezone.utc).isoformat(),
        "segment":          seg,
        "tfs":              tfs_out,
        "hourly_analysis":  hourly_by_tf,
        "fee_analysis":     fee_by_tf,
    }

    os.makedirs("data", exist_ok=True)
    out_path = os.path.join("data", f"{ticker}_config.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    # ── Resumen final ─────────────────────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  RESUMEN — {ticker} [{seg}]")
    print(f"  {'TF':>4}  {'Veredicto':>12}  {'N':>5}  {'WR':>4}  {'PF':>5}  "
          f"{'Mask':>12}  {'Scheme':>12}  vs_seg")
    print(f"  {'-'*72}")
    for tf_key in ["1m","5m","15m"]:
        res = best_by_tf.get(tf_key)
        if not res: print(f"  {tf_key:>4}  NO_DATA"); continue
        v  = res["verdict"]; bf = res["best_final"]; bs = res["baseline_s"]
        if v == "CUSTOM":
            s    = bf["stats"]
            d_pf = s["pf"] - bs.get("pf", 0)
            flag = "++" if d_pf > 0.2 else ("~" if abs(d_pf)<=0.2 else "--")
            print(f"  {tf_key:>4}  {'CUSTOM':>12}  {s['n']:>5}  {s['pct_win']:>3}%  "
                  f"{s['pf']:>5.2f}  {bf.get('mask','all'):>12}  "
                  f"{bf.get('scheme','default'):>12}  {flag} dPF={fmt(d_pf)}")
        elif v == "BLOCK":
            print(f"  {tf_key:>4}  {'BLOQUEAR':>12}  —  —  —  DESACTIVAR este TF")
        else:
            print(f"  {tf_key:>4}  {'USE_SEG':>12}  {bs.get('n',0):>5}  "
                  f"{bs.get('pct_win',0):>3}%  {bs.get('pf',0):>5.2f}  "
                  f"{'all':>12}  {'default':>12}  (sin datos suficientes)")

    print(f"\n  OUTPUT: {out_path}")
    print(f"  Sube al frontend: Watchlist -> Cargar Config -> pegar JSON -> Cargar")
    print(f"{'='*W}\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Per-Ticker Parameter Optimizer v2")
    parser.add_argument("ticker", help="Ticker a calibrar (ej: NVDA, PLTR, IONQ)")
    args = parser.parse_args()
    run(args.ticker.upper())
