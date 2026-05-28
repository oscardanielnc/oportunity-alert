#!/usr/bin/env python3
"""
Backtest multi-ticker / multi-fecha — compara v2 vs v3 del signal engine.

v2: score CERRAR>=4, sin time-stop, sin momentum adj, sin SIP threshold
v3: score CERRAR>=3, time-stop 15 scans, momentum adj, RSI override

Corre las mismas barras en ambas versiones y reporta por ticker/dia y global.
"""
import os, sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS

ALPACA_BASE = "https://data.alpaca.markets"
LIMA_OFFSET = -5
CONFIRM_REQ = 2

# ── Parametros v3 ──────────────────────────────────────────────────────────────
MAX_POSITION_SCANS = 15
ADVERSE_MOVE_PCT   = 0.3

# ── Tickers a testear (elegidos por volatilidad / relevancia) ──────────────────
TICKERS = ["MU", "NVDA", "PLTR", "IONQ", "QBTS", "AMD", "APP"]

# ── Fechas: ultimos 5 dias habiles antes de hoy (2026-05-27 es hoy) ───────────
# Dias habiles recientes: 27-may (hoy, parcial), 23, 22, 21, 20 mayo
DATES = [
    ("2026-05-27", "2026-05-27T08:00:00Z", "2026-05-27T04:00:00Z"),  # hoy
    ("2026-05-23", "2026-05-23T08:00:00Z", "2026-05-23T04:00:00Z"),
    ("2026-05-22", "2026-05-22T08:00:00Z", "2026-05-22T04:00:00Z"),
    ("2026-05-21", "2026-05-21T08:00:00Z", "2026-05-21T04:00:00Z"),
    ("2026-05-20", "2026-05-20T08:00:00Z", "2026-05-20T04:00:00Z"),
]


def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


def fetch(ticker, tf, start, end=None, limit=1000):
    bars, params = [], {
        "timeframe": tf, "start": start, "feed": "sip",
        "limit": limit, "sort": "asc",
    }
    if end:
        params["end"] = end
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                         params=params, headers=_hdrs(), timeout=30)
        r.raise_for_status()
        data = r.json()
        bars = list(data.get("bars") or [])
        while data.get("next_page_token"):
            params["page_token"] = data["next_page_token"]
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=_hdrs(), timeout=30)
            r.raise_for_status()
            data = r.json()
            bars.extend(data.get("bars") or [])
    except Exception as e:
        print(f"  [FETCH ERROR] {ticker} {tf}: {e}")
    return [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": int(b["v"])} for b in bars]


def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


def is_regular_hours(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    is_edt = 3 <= dt.month <= 11
    et = dt - timedelta(hours=4 if is_edt else 5)
    dm = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= dm < 16 * 60


# ── Simulacion principal ───────────────────────────────────────────────────────

def simulate(bars1, bars5, version="v3"):
    """
    Simula barra a barra. version='v2' o 'v3'.
    Retorna lista de trades: [{side, in_t, in_p, out_t, out_p, pnl, note}]
    """
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []

    for i in range(30, len(bars1)):
        candles   = bars1[:i + 1]
        bar       = candles[-1]
        cur_ts    = bar["t"]
        cur_price = bar["c"]

        # HTF
        htf_avail = [b for b in bars5 if b["t"] < cur_ts]
        htf_trend = compute_htf_trend(htf_avail) if len(htf_avail) >= 20 else "NEUTRAL"

        # ── v3: Time-stop ──────────────────────────────────────────────────────
        if version == "v3" and position:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep_pos = position["entry_price"]
            if position["side"] == "LONG":
                adverse = (ep_pos - cur_price) / ep_pos * 100
            else:
                adverse = (cur_price - ep_pos) / ep_pos * 100

            if position["scans_held"] >= MAX_POSITION_SCANS and adverse >= ADVERSE_MOVE_PCT:
                no = bars1[i + 1]["o"] if i + 1 < len(bars1) else cur_price
                nt = lima_ts(bars1[i + 1]) if i + 1 < len(bars1) else lima_ts(bar)
                side = position["side"]
                pnl  = (no - ep_pos) / ep_pos * 100 if side == "LONG" else (ep_pos - no) / ep_pos * 100
                trades.append({"side": side, "in_t": position["entry_time"], "in_p": ep_pos,
                                "out_t": nt, "out_p": no, "pnl": round(pnl, 3), "note": "TIME-STOP"})
                position = None; lock_dir = None; lock_rem = 0
                cur_sig = "ESPERAR"; cand_sig = None; cand_count = 0
                continue

        pos_state = "SIN_POSICION" if not position else position["side"]
        res       = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig   = res["signal"]

        # Direction lock
        if lock_rem > 0 and lock_dir:
            if (lock_dir == "SHORT" and raw_sig == "ENTRAR_SHORT") or \
               (lock_dir == "LONG"  and raw_sig == "ENTRAR_LONG"):
                raw_sig = "ESPERAR"
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0:
                lock_dir = None

        # ── Confirmacion ──────────────────────────────────────────────────────
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

        # Direction lock al confirmar
        if cur_sig == "ENTRAR_LONG":
            lock_dir = "SHORT"; lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig == "ENTRAR_SHORT":
            lock_dir = "LONG";  lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig in ("CERRAR_LONG", "CERRAR_SHORT", "ESPERAR"):
            lock_dir = None; lock_rem = 0

        ep = bars1[i + 1]["o"] if i + 1 < len(bars1) else bar["c"]
        et = lima_ts(bars1[i + 1]) if i + 1 < len(bars1) else lima_ts(bar)

        def close_pos(ep, et, note=""):
            if not position: return
            side = position["side"]
            pnl  = (ep - position["entry_price"]) / position["entry_price"] * 100
            if side == "SHORT": pnl = -pnl
            trades.append({"side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
                           "out_t": et, "out_p": ep, "pnl": round(pnl, 3), "note": note})

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"] == "SHORT":
                close_pos(ep, et, "inversion")
            if not position:
                position = {"side": "LONG", "entry_price": ep, "entry_time": et, "scans_held": 0}
        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"] == "LONG":
                close_pos(ep, et, "inversion")
            if not position:
                position = {"side": "SHORT", "entry_price": ep, "entry_time": et, "scans_held": 0}
        elif cur_sig == "CERRAR_LONG" and position and position["side"] == "LONG":
            close_pos(ep, et); position = None
        elif cur_sig == "CERRAR_SHORT" and position and position["side"] == "SHORT":
            close_pos(ep, et); position = None

    # Cerrar posicion abierta al final del dia
    if position:
        lp = bars1[-1]["c"]
        lt = lima_ts(bars1[-1]) + "*"
        side = position["side"]
        pnl  = (lp - position["entry_price"]) / position["entry_price"] * 100
        if side == "SHORT": pnl = -pnl
        trades.append({"side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
                       "out_t": lt, "out_p": lp, "pnl": round(pnl, 3), "note": "fin_dia"})

    return trades


# ── Estadisticas por batch de trades ──────────────────────────────────────────

def stats(trades):
    if not trades:
        return {"n": 0, "wins": 0, "pct_win": 0, "total_pnl": 0,
                "avg_pnl": 0, "max_win": 0, "max_loss": 0, "time_stops": 0}
    pnls      = [t["pnl"] for t in trades]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p < 0]
    tstops    = sum(1 for t in trades if t.get("note") == "TIME-STOP")
    total     = sum(pnls)
    return {
        "n":         len(trades),
        "wins":      len(wins),
        "pct_win":   round(len(wins) / len(trades) * 100, 0),
        "total_pnl": round(total, 2),
        "avg_pnl":   round(total / len(trades), 3),
        "max_win":   round(max(wins,   default=0), 2),
        "max_loss":  round(min(losses, default=0), 2),
        "time_stops": tstops,
    }


# ── Runner ─────────────────────────────────────────────────────────────────────

def run():
    W = 80
    print(f"\n{'='*W}")
    print(f"  BACKTEST MULTI-TICKER  |  {len(TICKERS)} tickers  x  {len(DATES)} dias")
    print(f"  Tickers: {', '.join(TICKERS)}")
    print(f"  Dias:    {' | '.join(d[0] for d in DATES)}")
    print(f"{'='*W}\n")

    all_v2 = []
    all_v3 = []

    # Tabla de resultados por ticker-dia
    header = f"  {'Ticker':<6} {'Fecha':<11} {'N_v2':>4} {'PnL_v2':>8} {'WR_v2':>6} | {'N_v3':>4} {'PnL_v3':>8} {'WR_v3':>6} {'TS':>4} {'Delta':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for ticker in TICKERS:
        for date_label, start1m, starthtf in DATES:
            # End del dia (23:59 UTC del mismo dia)
            end1m = date_label + "T23:59:00Z"

            bars1 = fetch(ticker, "1Min", start1m, end1m, 1000)
            bars5 = fetch(ticker, "5Min", starthtf, end1m, 300)

            if len(bars1) < 35:
                print(f"  {ticker:<6} {date_label:<11} -- sin datos suficientes ({len(bars1)} barras)")
                continue

            t_v2 = simulate(bars1, bars5, version="v2")
            t_v3 = simulate(bars1, bars5, version="v3")

            s2 = stats(t_v2)
            s3 = stats(t_v3)

            all_v2.extend(t_v2)
            all_v3.extend(t_v3)

            delta = s3["total_pnl"] - s2["total_pnl"]
            d_sign = "+" if delta >= 0 else ""
            wr2_s = f"{s2['pct_win']:.0f}%" if s2["n"] else "  -"
            wr3_s = f"{s3['pct_win']:.0f}%" if s3["n"] else "  -"
            pnl2_s = f"{'+' if s2['total_pnl']>=0 else ''}{s2['total_pnl']:.2f}%" if s2["n"] else "     -"
            pnl3_s = f"{'+' if s3['total_pnl']>=0 else ''}{s3['total_pnl']:.2f}%" if s3["n"] else "     -"
            ts_s = str(s3["time_stops"]) if s3["time_stops"] else " -"
            d_s  = f"{d_sign}{delta:.2f}%" if s2["n"] or s3["n"] else "   -"

            print(f"  {ticker:<6} {date_label:<11} {s2['n']:>4} {pnl2_s:>8} {wr2_s:>6} | "
                  f"{s3['n']:>4} {pnl3_s:>8} {wr3_s:>6} {ts_s:>4} {d_s:>8}")

    # ── Totales globales ───────────────────────────────────────────────────────
    g2 = stats(all_v2)
    g3 = stats(all_v3)
    print()
    print("  " + "=" * (W - 2))
    wr2g = f"{g2['pct_win']:.0f}%"
    wr3g = f"{g3['pct_win']:.0f}%"
    pnl2g = f"{'+' if g2['total_pnl']>=0 else ''}{g2['total_pnl']:.2f}%"
    pnl3g = f"{'+' if g3['total_pnl']>=0 else ''}{g3['total_pnl']:.2f}%"
    print(f"  {'GLOBAL':<6} {'TOTAL':<11} {g2['n']:>4} {pnl2g:>8} {wr2g:>6} | "
          f"{g3['n']:>4} {pnl3g:>8} {wr3g:>6} {g3['time_stops']:>4}")
    print()
    print(f"  RESUMEN GLOBAL v2 (sin time-stop):  {g2['n']} trades | WR {g2['pct_win']:.0f}% | "
          f"Total {'+' if g2['total_pnl']>=0 else ''}{g2['total_pnl']:.2f}% | "
          f"Avg {g2['avg_pnl']:+.3f}% | "
          f"Best {g2['max_win']:+.2f}% | Worst {g2['max_loss']:+.2f}%")
    print(f"  RESUMEN GLOBAL v3 (con time-stop): {g3['n']} trades | WR {g3['pct_win']:.0f}% | "
          f"Total {'+' if g3['total_pnl']>=0 else ''}{g3['total_pnl']:.2f}% | "
          f"Avg {g3['avg_pnl']:+.3f}% | "
          f"Best {g3['max_win']:+.2f}% | Worst {g3['max_loss']:+.2f}% | "
          f"Time-stops: {g3['time_stops']}")
    delta_global = g3["total_pnl"] - g2["total_pnl"]
    print(f"\n  DELTA v3 vs v2  =  {'+' if delta_global>=0 else ''}{delta_global:.2f}pp  "
          f"({'+' if delta_global>=0 else ''}{'mejora' if delta_global>=0 else 'empeora'})")

    # ── Peores trades por version ──────────────────────────────────────────────
    print()
    worst_v2 = sorted(all_v2, key=lambda t: t["pnl"])[:5]
    worst_v3 = sorted(all_v3, key=lambda t: t["pnl"])[:5]
    print("  PEORES TRADES v2 (top 5 perdedores):")
    for t in worst_v2:
        print(f"    {t['side']:<5} {t['in_t']}->{t['out_t']}  ${t['in_p']:.2f}->${t['out_p']:.2f}  {t['pnl']:+.3f}%  [{t.get('note','')}]")
    print()
    print("  PEORES TRADES v3 (top 5 perdedores):")
    for t in worst_v3:
        print(f"    {t['side']:<5} {t['in_t']}->{t['out_t']}  ${t['in_p']:.2f}->${t['out_p']:.2f}  {t['pnl']:+.3f}%  [{t.get('note','')}]")
    print()


if __name__ == "__main__":
    run()
