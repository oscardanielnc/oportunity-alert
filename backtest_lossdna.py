#!/usr/bin/env python3
"""
Loss DNA Analysis — anatomia de wins vs losses por criterio de entrada
======================================================================
Para cada trade captura los indicadores y criterios activos AL MOMENTO
de la entrada, luego analiza que combinaciones predicen wins vs losses.

Analisis:
  1. Win rate por SCORE total (4-9)
  2. Impacto de cada criterio individual (L1-L6 / S1-S6)
  3. Win rate por HTF trend al entrar
  4. Win rate por hora del dia (Lima)
  5. Win rate por ticker
  6. Win rate LONG vs SHORT
  7. Win rate por rango RSI al entrar
  8. Impacto de patrones de velas (sin patron / score 1-2 / score 3-4)
  9. FILTROS SUGERIDOS: criterios y combinaciones net-negativas

Usa los 3 TF con sus parametros optimizados (2026-05-28):
  1m: 20sc/0.5%  |  5m: 12sc/0.0%  |  15m: 4sc/1.0%
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

TF_CONFIG = {
    "1m":  {"tf": "1Min",  "htf": "5Min",  "weeks": 1,  "warmup": 30, "max_scans": 20, "adverse": 0.5},
    "5m":  {"tf": "5Min",  "htf": "15Min", "weeks": 6,  "warmup": 30, "max_scans": 12, "adverse": 0.0},
    "15m": {"tf": "15Min", "htf": "1Hour", "weeks": 12, "warmup": 30, "max_scans":  4, "adverse": 1.0},
}

LIMA = timezone(timedelta(hours=LIMA_OFFSET))


# ── API ──────────────────────────────────────────────────────────────────────────

def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }

def fetch_all(ticker, tf, start, limit=10000):
    bars, params = [], {"timeframe": tf, "start": start, "feed": "sip", "limit": 1000, "sort": "asc"}
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
            _time.sleep(0.15)
    except Exception:
        pass
    return [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": int(b["v"])} for b in bars]


# ── Helpers ───────────────────────────────────────────────────────────────────────

def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")

def lima_hour(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).hour


# ── Simulacion con captura de criterios ──────────────────────────────────────────

def simulate_with_dna(bars, htf_bars, ticker, tf_key, warmup, max_scans, adverse_pct):
    """
    Igual que simulate() pero captura un 'dna_record' en cada entrada con todos
    los indicadores y criterios activos al momento de la entrada.
    Retorna (trades, dna_records) donde dna_records[i] corresponde a trades[i].
    """
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []
    dna_recs   = []
    htf_ptr    = 0
    pending_dna = None   # dna del candidato pendiente de confirmacion

    for i in range(warmup, len(bars)):
        bar       = bars[i]
        cur_ts    = bar["t"]
        cur_price = bar["c"]
        candles   = bars[max(0, i - 119):i + 1]

        while htf_ptr < len(htf_bars) and htf_bars[htf_ptr]["t"] < cur_ts:
            htf_ptr += 1
        htf_slice = htf_bars[max(0, htf_ptr - 50):htf_ptr]
        htf_trend = compute_htf_trend(htf_slice) if len(htf_slice) >= 20 else "NEUTRAL"

        # Time-stop
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
                trades.append({"side": side, "in_t": position["entry_time"], "in_p": ep_pos,
                                "out_t": nt, "out_p": no, "pnl": round(pnl, 3),
                                "note": "TIME-STOP", "ticker": ticker, "tf": tf_key})
                dna_recs.append(position.get("dna"))
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

        # Capturar DNA del candidato cuando aparece la senal
        if raw_sig in ("ENTRAR_LONG", "ENTRAR_SHORT") and raw_sig != cand_sig:
            crit = res.get("criteria", {})
            ind  = res.get("indicators", {})
            side_key = "LONG" if raw_sig == "ENTRAR_LONG" else "SHORT"
            pending_dna = {
                "ticker":     ticker,
                "tf":         tf_key,
                "side":       side_key,
                "score":      res["score_long"] if side_key == "LONG" else res["score_short"],
                "htf":        htf_trend,
                "hour":       lima_hour(bar),
                "rsi":        ind.get("rsi", 50.0),
                "bb_pos":     ind.get("bb_position", "inside"),
                "vol_ratio":  ind.get("vol_ratio", 1.0),
                "macd_hist":  ind.get("macd_hist", 0.0),
                # Criterios individuales (segun direccion)
                "C1": crit.get("L1" if side_key == "LONG" else "S1", False),
                "C2": crit.get("L2" if side_key == "LONG" else "S2", False),
                "C3": crit.get("L3" if side_key == "LONG" else "S3", False),
                "C4": crit.get("L4" if side_key == "LONG" else "S4", False),
                "C6": crit.get("L6" if side_key == "LONG" else "S6", False),
                "pat_score":  crit.get("bull_pscore" if side_key == "LONG" else "bear_pscore", 0),
                "has_pat":    bool(crit.get("bull_patterns" if side_key == "LONG" else "bear_patterns")),
                "n_criteria": sum([crit.get("L1" if side_key == "LONG" else "S1", False),
                                   crit.get("L2" if side_key == "LONG" else "S2", False),
                                   crit.get("L3" if side_key == "LONG" else "S3", False),
                                   crit.get("L4" if side_key == "LONG" else "S4", False),
                                   crit.get("L6" if side_key == "LONG" else "S6", False)]),
            }
        elif raw_sig not in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            pending_dna = None

        # Confirmacion
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
            trades.append({"side": side, "in_t": p["entry_time"], "in_p": p["entry_price"],
                           "out_t": et, "out_p": ep, "pnl": round(pnl, 3), "note": note,
                           "ticker": ticker, "tf": tf_key})
            dna_recs.append(p.get("dna"))

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"] == "SHORT":
                close_pos(ep, et, "inversion", position); position = None
            if not position:
                position = {"side": "LONG", "entry_price": ep, "entry_time": et,
                            "scans_held": 0, "dna": pending_dna}
        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"] == "LONG":
                close_pos(ep, et, "inversion", position); position = None
            if not position:
                position = {"side": "SHORT", "entry_price": ep, "entry_time": et,
                            "scans_held": 0, "dna": pending_dna}
        elif cur_sig == "CERRAR_LONG" and position and position["side"] == "LONG":
            close_pos(ep, et, "", position); position = None
        elif cur_sig == "CERRAR_SHORT" and position and position["side"] == "SHORT":
            close_pos(ep, et, "", position); position = None

    if position:
        lp = bars[-1]["c"]; lt = lima_ts(bars[-1]) + "*"
        side = position["side"]
        pnl  = (lp - position["entry_price"]) / position["entry_price"] * 100
        if side == "SHORT": pnl = -pnl
        trades.append({"side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
                       "out_t": lt, "out_p": lp, "pnl": round(pnl, 3), "note": "fin_periodo",
                       "ticker": ticker, "tf": tf_key})
        dna_recs.append(position.get("dna"))

    return trades, dna_recs


# ── Analisis ──────────────────────────────────────────────────────────────────────

def wr_avg(records):
    """(win_rate_pct, avg_pnl, n, profit_factor)"""
    if not records:
        return 0.0, 0.0, 0, 0.0
    pnls   = [r["pnl"] for r in records]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins);   gross_l = abs(sum(losses))
    pf = round(gross_w / gross_l, 2) if gross_l > 0 else (99.0 if gross_w > 0 else 0.0)
    return (round(len(wins) / len(records) * 100),
            round(sum(pnls) / len(records), 3),
            len(records), pf)

def flag(wr, base_wr):
    if wr >= base_wr + 8:  return " ++ BUENO"
    if wr >= base_wr + 4:  return " + bueno"
    if wr <= base_wr - 8:  return " -- MALO"
    if wr <= base_wr - 4:  return " - malo"
    return ""

def hdr(title, W=88):
    print(f"\n  {'='*W}")
    print(f"  {title}")
    print(f"  {'='*W}")


# ── Runner ────────────────────────────────────────────────────────────────────────

def run():
    W = 88
    now_utc = datetime.now(timezone.utc)
    label   = (now_utc + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*W}")
    print(f"  LOSS DNA ANALYSIS  --  {label} Lima")
    print(f"  Tickers: {', '.join(TICKERS)}")
    print(f"  TFs: 1m (1sem)  5m (6sem)  15m (12sem)  -- params optimizados")
    print(f"{'='*W}")

    all_pairs = []   # (trade, dna) para analisis combinado

    for tf_key, cfg in TF_CONFIG.items():
        print(f"\n[{tf_key.upper()}] Cargando datos...")
        start_dt  = (now_utc - timedelta(weeks=cfg["weeks"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start = (now_utc - timedelta(weeks=cfg["weeks"] + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        ticker_data = {}
        for ticker in TICKERS:
            sys.stdout.write(f"  {ticker}... "); sys.stdout.flush()
            bars     = fetch_all(ticker, cfg["tf"],  start_dt)
            htf_bars = fetch_all(ticker, cfg["htf"], htf_start)
            if len(bars) >= cfg["warmup"] + 5:
                ticker_data[ticker] = (bars, htf_bars)
                print(f"{len(bars)} barras")
            else:
                print(f"skip")

        for ticker, (bars, htf_bars) in ticker_data.items():
            trades, dnas = simulate_with_dna(
                bars, htf_bars, ticker, tf_key,
                cfg["warmup"], cfg["max_scans"], cfg["adverse"])
            for t, d in zip(trades, dnas):
                if d:  # solo trades con DNA capturado
                    all_pairs.append({"trade": t, "dna": d})

    if not all_pairs:
        print("Sin trades con DNA. Saliendo.")
        return

    print(f"\n  Total trades con DNA capturado: {len(all_pairs)}")
    wins_total  = sum(1 for p in all_pairs if p["trade"]["pnl"] > 0)
    base_wr     = round(wins_total / len(all_pairs) * 100)
    base_avg    = round(sum(p["trade"]["pnl"] for p in all_pairs) / len(all_pairs), 3)
    gross_w = sum(p["trade"]["pnl"] for p in all_pairs if p["trade"]["pnl"] > 0)
    gross_l = abs(sum(p["trade"]["pnl"] for p in all_pairs if p["trade"]["pnl"] <= 0))
    base_pf = round(gross_w / gross_l, 2) if gross_l > 0 else 0.0
    print(f"  BASELINE GLOBAL: WR {base_wr}%  Avg {base_avg:+.3f}%  PF {base_pf:.2f}")

    def pairs_where(fn):
        return [p for p in all_pairs if fn(p["dna"])]

    # =========================================================================
    hdr("1. WIN RATE POR SCORE TOTAL (cuantos criterios activos = cuanto gana)")
    print(f"  {'Score':>5}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  Comparado con baseline")
    print(f"  {'-'*58}")
    for sc in range(4, 10):
        sub = pairs_where(lambda d, s=sc: d["score"] == s)
        if not sub: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        print(f"  {sc:>5}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")
    # High scores combined
    sub_hi = pairs_where(lambda d: d["score"] >= 7)
    if sub_hi:
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub_hi])
        print(f"  {'7+':>5}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("2. IMPACTO DE CADA CRITERIO INDIVIDUAL (cuando activo vs cuando ausente)")
    print(f"  {'Criterio':<22}  {'--- ACTIVO ---':^26}  {'--- AUSENTE ---':^26}  Delta WR")
    print(f"  {' ':22}  {'N':>4} {'WR':>4} {'Avg':>7} {'PF':>5}  {'N':>4} {'WR':>4} {'Avg':>7} {'PF':>5}  ")
    print(f"  {'-'*82}")

    CRITERIA = [
        ("C1 (BB extremo)",  "C1"),
        ("C2 (RSI extremo)", "C2"),
        ("C3 (MACD mejor)",  "C3"),
        ("C4 (S/R cerca)",   "C4"),
        ("C6 (Volumen)",     "C6"),
        ("Patron velas",     "has_pat"),
    ]
    for label_c, key in CRITERIA:
        active  = pairs_where(lambda d, k=key: bool(d.get(k)))
        absent  = pairs_where(lambda d, k=key: not bool(d.get(k)))
        if len(active) < 5 or len(absent) < 5:
            continue
        wa, aa, na, pfa = wr_avg([p["trade"] for p in active])
        wb, ab, nb, pfb = wr_avg([p["trade"] for p in absent])
        delta = wa - wb
        delta_s = f"{'+' if delta>=0 else ''}{delta}pp"
        tag = " ++ CONFIRMA" if delta >= 8 else (" -- PERJUDICA" if delta <= -8 else "")
        print(f"  {label_c:<22}  {na:>4} {wa:>3}% {aa:>+7.3f}% {pfa:>5.2f}  "
              f"{nb:>4} {wb:>3}% {ab:>+7.3f}% {pfb:>5.2f}  {delta_s:>7}{tag}")

    # =========================================================================
    hdr("3. WIN RATE POR NUMERO DE CRITERIOS BASE ACTIVOS (sin contar patrones)")
    print(f"  {'N criterios':>11}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  Notas")
    print(f"  {'-'*60}")
    for nc in range(0, 6):
        sub = pairs_where(lambda d, n=nc: d["n_criteria"] == n)
        if len(sub) < 3: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        print(f"  {nc:>11}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("4. IMPACTO DE PATRONES DE VELAS POR FUERZA (score 0 / 1-2 / 3-4)")
    print(f"  {'Patron score':>13}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  Notas")
    print(f"  {'-'*55}")
    for ps_label, fn in [("Sin patron (0)",  lambda d: d["pat_score"] == 0),
                          ("Debil   (1-2)",   lambda d: 1 <= d["pat_score"] <= 2),
                          ("Fuerte  (3-4)",   lambda d: d["pat_score"] >= 3)]:
        sub = pairs_where(fn)
        if len(sub) < 5: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        print(f"  {ps_label:>13}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("5. WIN RATE POR HTF TREND AL MOMENTO DE ENTRADA")
    print(f"  {'HTF trend':>12}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  Notas")
    print(f"  {'-'*52}")
    for htf in ["BULLISH", "NEUTRAL", "BEARISH"]:
        for side in ["LONG", "SHORT"]:
            sub = pairs_where(lambda d, h=htf, s=side: d["htf"] == h and d["side"] == s)
            if len(sub) < 5: continue
            wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
            lbl = f"{htf}/{side}"
            print(f"  {lbl:>12}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("6. WIN RATE POR HORA DEL DIA (Lima)")
    print(f"  {'Hora Lima':>9}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  Notas")
    print(f"  {'-'*52}")
    hour_groups = [
        ("03-07h (pre)",  lambda d: 3  <= d["hour"] < 7),
        ("07-09h (pre2)", lambda d: 7  <= d["hour"] < 9),
        ("09-11h (open)", lambda d: 9  <= d["hour"] < 11),
        ("11-13h (mid)",  lambda d: 11 <= d["hour"] < 13),
        ("13-15h (pm1)",  lambda d: 13 <= d["hour"] < 15),
        ("15-17h (close)",lambda d: 15 <= d["hour"] < 17),
    ]
    for lbl, fn in hour_groups:
        sub = pairs_where(fn)
        if len(sub) < 5: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        print(f"  {lbl:>9}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("7. WIN RATE POR RANGO DE RSI AL ENTRAR")
    print(f"  {'RSI rango':>14}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  LONG vs SHORT")
    print(f"  {'-'*60}")
    rsi_bins_long  = [("<25", lambda d: d["rsi"] < 25  and d["side"]=="LONG"),
                      ("25-35", lambda d: 25 <= d["rsi"] < 35 and d["side"]=="LONG"),
                      ("35-45", lambda d: 35 <= d["rsi"] < 45 and d["side"]=="LONG"),
                      ("45-55", lambda d: 45 <= d["rsi"] < 55 and d["side"]=="LONG"),
                      ("55-65", lambda d: 55 <= d["rsi"] < 65 and d["side"]=="LONG"),
                      (">65",   lambda d: d["rsi"] >= 65 and d["side"]=="LONG"),
                      (">65 SH",lambda d: d["rsi"] >= 65 and d["side"]=="SHORT"),
                      ("55-65 SH",lambda d: 55<=d["rsi"]<65 and d["side"]=="SHORT"),
                      ("<35 SH",lambda d: d["rsi"] < 35 and d["side"]=="SHORT")]
    for lbl, fn in rsi_bins_long:
        sub = pairs_where(fn)
        if len(sub) < 5: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        print(f"  {lbl:>14}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("8. WIN RATE POR TICKER (para identificar cuales arrastran el PF)")
    print(f"  {'Ticker':>7}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  Mejor TF")
    print(f"  {'-'*50}")
    ticker_results = []
    for ticker in TICKERS:
        sub = [p for p in all_pairs if p["dna"]["ticker"] == ticker]
        if len(sub) < 5: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        ticker_results.append((ticker, wr, avg, n, pf))
    ticker_results.sort(key=lambda x: -x[4])  # by PF
    for ticker, wr, avg, n, pf in ticker_results:
        print(f"  {ticker:>7}  {n:>5}  {wr:>4}%  {avg:>+8.3f}%  {pf:>5.2f}  {flag(wr, base_wr)}")

    # =========================================================================
    hdr("9. WIN RATE POR TIMEFRAME")
    for tf_key in TF_CONFIG:
        sub = [p for p in all_pairs if p["dna"]["tf"] == tf_key]
        if not sub: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        long_sub  = [p for p in sub if p["dna"]["side"] == "LONG"]
        short_sub = [p for p in sub if p["dna"]["side"] == "SHORT"]
        wr_l = wr_avg([p["trade"] for p in long_sub])[0]  if long_sub  else 0
        wr_s = wr_avg([p["trade"] for p in short_sub])[0] if short_sub else 0
        print(f"  [{tf_key.upper()}]  n={n}  WR={wr}%  Avg={avg:+.3f}%  PF={pf:.2f}  "
              f"LONG={wr_l}%({len(long_sub)})  SHORT={wr_s}%({len(short_sub)})")

    # =========================================================================
    hdr("10. COMBINACIONES PROBLEMATICAS (pares de criterios con WR baja)")
    print("  Analiza pares: cuando dos criterios aparecen JUNTOS vs solo uno")
    print(f"  {'Criterios activos':^30}  {'N':>5}  {'WR':>5}  {'Avg PnL':>8}  {'PF':>5}  vs solo-C1")
    print(f"  {'-'*70}")

    crit_keys = ["C1","C2","C3","C4","C6"]
    pair_results = []
    for ci in range(len(crit_keys)):
        for cj in range(ci+1, len(crit_keys)):
            k1, k2 = crit_keys[ci], crit_keys[cj]
            both = pairs_where(lambda d, a=k1, b=k2: d.get(a) and d.get(b))
            only1 = pairs_where(lambda d, a=k1, b=k2: d.get(a) and not d.get(b))
            if len(both) < 8: continue
            wr_b, avg_b, n_b, pf_b = wr_avg([p["trade"] for p in both])
            wr_1, _, n_1, _ = wr_avg([p["trade"] for p in only1]) if only1 else (0,0,0,0)
            delta = wr_b - base_wr
            pair_results.append((k1, k2, wr_b, avg_b, n_b, pf_b, delta, wr_1))

    # Sort por WR (peores primero, luego mejores)
    pair_results.sort(key=lambda x: x[2])
    print("  -- PEORES combinaciones (arrastran el WR):")
    for k1, k2, wr_b, avg_b, n_b, pf_b, delta, wr_1 in pair_results[:5]:
        tag = " -- EVITAR" if delta <= -8 else ""
        print(f"  {k1}+{k2:^26}  {n_b:>5}  {wr_b:>4}%  {avg_b:>+8.3f}%  {pf_b:>5.2f}  "
              f"d={'+' if delta>=0 else ''}{delta}pp{tag}")
    print("  -- MEJORES combinaciones:")
    for k1, k2, wr_b, avg_b, n_b, pf_b, delta, wr_1 in pair_results[-5:]:
        tag = " ++ SINERGIAN" if delta >= 8 else ""
        print(f"  {k1}+{k2:^26}  {n_b:>5}  {wr_b:>4}%  {avg_b:>+8.3f}%  {pf_b:>5.2f}  "
              f"d={'+' if delta>=0 else ''}{delta}pp{tag}")

    # =========================================================================
    hdr("11. FILTROS SUGERIDOS (basados en datos)")
    print()

    suggestions = []

    # Score filter
    sc4_sub = pairs_where(lambda d: d["score"] == 4)
    if sc4_sub:
        wr4, avg4, n4, pf4 = wr_avg([p["trade"] for p in sc4_sub])
        if wr4 < base_wr - 5:
            suggestions.append(
                f"  SCORE 4 (n={n4}, WR={wr4}%, PF={pf4:.2f}): elevar umbral minimo a 5 en los 3 TF."
                f" Eliminaria {n4} trades con WR {base_wr-wr4}pp por debajo del baseline.")

    # Criterion filter
    for label_c, key in CRITERIA:
        active = pairs_where(lambda d, k=key: bool(d.get(k)))
        absent = pairs_where(lambda d, k=key: not bool(d.get(k)))
        if len(active) < 10 or len(absent) < 10: continue
        wa, aa, na, pfa = wr_avg([p["trade"] for p in active])
        wb, ab, nb, pfb = wr_avg([p["trade"] for p in absent])
        if wa < base_wr - 8:
            suggestions.append(
                f"  {label_c} (n={na}, WR={wa}% activo vs {wb}% ausente): "
                f"cuando activo SOLO, considerar requerir al menos 1 criterio adicional.")
        if wa > base_wr + 8:
            suggestions.append(
                f"  {label_c} (n={na}, WR={wa}%, PF={pfa:.2f}): criterio FUERTE. "
                f"Considerar bajar umbral de 1pt cuando este activo.")

    # Hour filter
    for lbl, fn in hour_groups:
        sub = pairs_where(fn)
        if len(sub) < 10: continue
        wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
        if wr < base_wr - 10:
            suggestions.append(
                f"  HORA {lbl} (n={n}, WR={wr}%): zona de bajo rendimiento. "
                f"Considerar elevar threshold de entrada en esta ventana horaria.")

    # HTF filter
    for htf in ["NEUTRAL", "BEARISH", "BULLISH"]:
        for side in ["LONG", "SHORT"]:
            sub = pairs_where(lambda d, h=htf, s=side: d["htf"]==h and d["side"]==s)
            if len(sub) < 15: continue
            wr, avg, n, pf = wr_avg([p["trade"] for p in sub])
            if wr < base_wr - 12:
                suggestions.append(
                    f"  HTF {htf} + {side} (n={n}, WR={wr}%): entrada {side} con mercado "
                    f"{htf} rinde {base_wr-wr}pp por debajo del baseline. "
                    f"El HTF filter ya lo penaliza — considerar +1pt adicional.")

    # Pattern filter
    no_pat = pairs_where(lambda d: d["pat_score"] == 0)
    weak_pat = pairs_where(lambda d: 1 <= d["pat_score"] <= 2)
    if len(no_pat) >= 10 and len(weak_pat) >= 10:
        wr_np, _, n_np, pf_np = wr_avg([p["trade"] for p in no_pat])
        wr_wp, _, n_wp, pf_wp = wr_avg([p["trade"] for p in weak_pat])
        if wr_np < base_wr - 5:
            suggestions.append(
                f"  SIN PATRON (n={n_np}, WR={wr_np}%, PF={pf_np:.2f}): trades sin ningun patron "
                f"rinden {base_wr-wr_np}pp menos. Considerar requerir patron como criterio adicional "
                f"(especialmente en 5m donde el sistema es mas debil).")

    # Ticker filter
    bad_tickers = [(t, wr, n, pf) for (t, wr, avg, n, pf) in ticker_results if pf < 0.9 and n >= 10]
    if bad_tickers:
        for t, wr, n, pf in bad_tickers:
            suggestions.append(
                f"  TICKER {t} (n={n}, WR={wr}%, PF={pf:.2f}): rendimiento sistematicamente "
                f"bajo. Considerar remover del watchlist o usar threshold +1.")

    if suggestions:
        for s in suggestions:
            print(s)
            print()
    else:
        print("  No hay filtros concluyentes con los datos disponibles.")
        print("  (Necesitas mas trades para mayor significancia estadistica)")

    print(f"\n  NOTA: todas las sugerencias son hipotesis — verificar con backtest A/B antes de implementar.")
    print()


if __name__ == "__main__":
    run()
