import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
Backtest Signal Engine v3 — MU desde 3am Lima (8am UTC) hoy.
Replica barra a barra: confirmacion (2 scans), direction lock, HTF trend.
Mejoras v3:
  - Time-stop: cierre automatico si posicion lleva >MAX_POSITION_SCANS scans
               Y el precio se movio >0.3% contra la entrada.
  - RSI override + CERRAR en score>=3 ya estan en el motor (signal_engine v3).
  - SIP threshold: fuera de horario regular (pre-market/AH) se exige 7/9 para entrar.
  - Momentum filter: ya en el motor (penaliza patrones debiles contra tendencia fuerte).
Entrada: siguiente barra open. Salida: siguiente barra open en CERRAR/inversion.
"""
import os, sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS

ALPACA_BASE        = "https://data.alpaca.markets"
TICKER             = "MU"
INTERVAL           = "1m"
START_1M           = "2026-05-27T08:00:00Z"   # 3am Lima = 8am UTC
START_HTF          = "2026-05-27T04:00:00Z"   # Contexto extra para EMA20 en 5m
CONFIRM_REQ        = 2
LIMA_OFFSET        = -5   # horas
MAX_POSITION_SCANS = 15   # scans max antes de time-stop si P&L negativo
ADVERSE_MOVE_PCT   = 0.3  # % movimiento adverso para activar time-stop
SIP_ENTRY_THRESH   = 5    # threshold de entrada fuera de horario regular (= regular; time-stop protege de posiciones largas)

# Horario regular ET: 9:30am-4pm ET = 14:30-21:00 UTC
REGULAR_START_UTC = timedelta(hours=14, minutes=30)
REGULAR_END_UTC   = timedelta(hours=21)


def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


def fetch(ticker, tf, start, feed="sip", limit=10000):
    bars, params = [], {
        "timeframe": tf, "start": start, "feed": feed, "limit": limit, "sort": "asc",
    }
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
    return [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": int(b["v"])} for b in bars]


def lima(bar, fmt="%H:%M"):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime(fmt)


def is_regular_hours(bar):
    """Returns True if bar timestamp falls within regular US market hours (9:30-16:00 ET)."""
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    # EDT (UTC-4) Mar-Nov, EST (UTC-5) Dec-Feb — May 27 is EDT
    is_edt = 3 <= dt.month <= 11
    offset = timedelta(hours=4 if is_edt else 5)
    et = dt - offset  # convert UTC to ET (subtract offset to go from UTC to ET)
    # Actually ET = UTC - 4 (EDT), so ET_hour = UTC_hour - 4
    # But timedelta subtraction: dt is UTC, et = dt - 4h gives ET time
    day_minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= day_minutes < 16 * 60


def run():
    W = 68
    print(f"\n{'='*W}")
    print(f"  BACKTEST {TICKER} - {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (3am Lima - ahora)")
    print(f"{'='*W}\n")

    print("Descargando barras Alpaca SIP...")
    bars1 = fetch(TICKER, "1Min", START_1M)
    bars5 = fetch(TICKER, "5Min", START_HTF)
    print(f"  {len(bars1)} barras 1m  |  {len(bars5)} barras 5m\n")

    if len(bars1) < 35:
        print("ERROR: barras insuficientes para backtest"); return

    # ── Estado de la simulacion ────────────────────────────────────────────────
    cand_sig      = None
    cand_count    = 0
    cur_sig       = "ESPERAR"
    lock_rem      = 0
    lock_dir      = None
    position      = None   # {"side", "entry_price", "entry_time", "scans_held"}
    time_stopped  = []     # log de time-stops ejecutados

    signals = []
    trades  = []

    # ── Loop barra a barra ─────────────────────────────────────────────────────
    for i in range(30, len(bars1)):
        candles   = bars1[:i + 1]
        bar       = candles[-1]
        cur_ts    = bar["t"]
        cur_price = bar["c"]

        # HTF: solo barras 5m completadas antes del timestamp actual
        htf_avail = [b for b in bars5 if b["t"] < cur_ts]
        htf_trend = compute_htf_trend(htf_avail) if len(htf_avail) >= 20 else "NEUTRAL"

        # ── Time-stop: cerrar si llevamos demasiados scans en perdida ──────────
        if position:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep_pos = position["entry_price"]
            if position["side"] == "LONG":
                adverse = (ep_pos - cur_price) / ep_pos * 100
            else:
                adverse = (cur_price - ep_pos) / ep_pos * 100

            if position["scans_held"] >= MAX_POSITION_SCANS and adverse >= ADVERSE_MOVE_PCT:
                # Time-stop disparado
                next_open = bars1[i + 1]["o"] if i + 1 < len(bars1) else cur_price
                next_time = lima(bars1[i + 1]) if i + 1 < len(bars1) else lima(bar)
                side = position["side"]
                if side == "LONG":
                    pnl = (next_open - ep_pos) / ep_pos * 100
                else:
                    pnl = (ep_pos - next_open) / ep_pos * 100
                trades.append({
                    "n": len(trades)+1, "side": side,
                    "in_t": position["entry_time"], "in_p": ep_pos,
                    "out_t": next_time, "out_p": next_open,
                    "pnl": round(pnl, 2), "note": "TIME-STOP (%d scans, -%.2f%%)" % (position["scans_held"], adverse),
                })
                time_stopped.append(lima(bar))
                position = None
                lock_dir = None; lock_rem = 0
                cur_sig  = "ESPERAR"
                cand_sig = None; cand_count = 0
                continue

        pos_state = "SIN_POSICION" if not position else position["side"]
        res       = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig   = res["signal"]

        # Aplicar direction lock
        if lock_rem > 0 and lock_dir:
            if (lock_dir == "SHORT" and raw_sig == "ENTRAR_SHORT") or \
               (lock_dir == "LONG"  and raw_sig == "ENTRAR_LONG"):
                raw_sig = "ESPERAR"

        # Decrementar lock
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0:
                lock_dir = None

        # SIP threshold: fuera de horario regular se exige mas conviccion para entrar
        if not is_regular_hours(bar) and pos_state == "SIN_POSICION":
            sl = res.get("score_long", 0)
            ss = res.get("score_short", 0)
            if raw_sig == "ENTRAR_LONG"  and sl < SIP_ENTRY_THRESH:
                raw_sig = "ESPERAR"
            if raw_sig == "ENTRAR_SHORT" and ss < SIP_ENTRY_THRESH:
                raw_sig = "ESPERAR"

        # Logica de confirmacion (2 scans consecutivos)
        confirmed = False
        if raw_sig == cand_sig:
            cand_count += 1
            if cand_count >= CONFIRM_REQ and raw_sig != cur_sig:
                confirmed  = True
                cur_sig    = raw_sig
            if cand_count >= CONFIRM_REQ:
                cand_sig = None; cand_count = 0
        else:
            cand_sig   = raw_sig
            cand_count = 1

        if not confirmed:
            continue

        # Aplicar direction lock tras confirmar ENTRAR
        if cur_sig == "ENTRAR_LONG":
            lock_dir = "SHORT"; lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig == "ENTRAR_SHORT":
            lock_dir = "LONG";  lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig in ("CERRAR_LONG", "CERRAR_SHORT", "ESPERAR"):
            lock_dir = None;    lock_rem = 0

        sl   = res.get("score_long", 0)
        ss   = res.get("score_short", 0)
        rsi  = res.get("indicators", {}).get("rsi", 50)
        pat  = res.get("patterns", {})
        bp   = ", ".join(pat.get("bull_patterns", [])[:2])
        brp  = ", ".join(pat.get("bear_patterns", [])[:2])
        pats = f"[{bp}]" if bp else (f"[{brp}]" if brp else "")

        next_open = bars1[i + 1]["o"] if i + 1 < len(bars1) else bar["c"]
        next_time = lima(bars1[i + 1]) if i + 1 < len(bars1) else lima(bar)

        signals.append({
            "time": lima(bar), "signal": cur_sig,
            "price": bar["c"], "next_open": next_open, "next_time": next_time,
            "bar_idx": i, "sl": sl, "ss": ss, "rsi": rsi,
            "htf": htf_trend, "pats": pats,
        })

        # ── Gestión de trades ──────────────────────────────────────────────────
        ep = next_open
        et = next_time

        def close_position(ep, et, note=""):
            if not position: return
            side = position["side"]
            if side == "LONG":
                pnl = (ep - position["entry_price"]) / position["entry_price"] * 100
            else:
                pnl = (position["entry_price"] - ep) / position["entry_price"] * 100
            trades.append({
                "n": len(trades)+1, "side": side,
                "in_t": position["entry_time"], "in_p": position["entry_price"],
                "out_t": et, "out_p": ep,
                "pnl": round(pnl, 2), "note": note,
            })

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"] == "SHORT":
                close_position(ep, et, "cerrado por inversion")
            if not position:
                position = {"side": "LONG", "entry_price": ep, "entry_time": et, "scans_held": 0}

        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"] == "LONG":
                close_position(ep, et, "cerrado por inversion")
            if not position:
                position = {"side": "SHORT", "entry_price": ep, "entry_time": et, "scans_held": 0}

        elif cur_sig == "CERRAR_LONG" and position and position["side"] == "LONG":
            close_position(ep, et)
            position = None

        elif cur_sig == "CERRAR_SHORT" and position and position["side"] == "SHORT":
            close_position(ep, et)
            position = None

    # Cerrar posición abierta al final
    if position:
        lp = bars1[-1]["c"]
        lt = lima(bars1[-1]) + "*"
        side = position["side"]
        pnl  = (lp - position["entry_price"]) / position["entry_price"] * 100
        if side == "SHORT": pnl = -pnl
        trades.append({
            "n": len(trades)+1, "side": side,
            "in_t": position["entry_time"], "in_p": position["entry_price"],
            "out_t": lt, "out_p": lp,
            "pnl": round(pnl, 2), "note": "posicion abierta al final",
        })

    # ── Imprimir señales ───────────────────────────────────────────────────────
    print("SEÑALES CONFIRMADAS:")
    print("-" * W)
    if not signals:
        print("  Sin señales confirmadas en el período.")
    for s in signals:
        score = f"L:{s['sl']}/9" if "LONG" in s["signal"] else (f"S:{s['ss']}/9" if "SHORT" in s["signal"] else f"L:{s['sl']}/9 S:{s['ss']}/9")
        htf_t = f" | HTF:{s['htf']}" if s["htf"] != "NEUTRAL" else ""
        pat_t = f" | {s['pats']}" if s["pats"] else ""
        print(f"  {s['time']}  {s['signal']:<16} ${s['price']:.2f}  RSI:{s['rsi']:.0f}  {score}{htf_t}{pat_t}")
        print(f"           > Ejecutar en siguiente bar: ${s['next_open']:.2f} @ {s['next_time']}")

    print()

    # ── Imprimir trades ────────────────────────────────────────────────────────
    print("TRADES EJECUTADOS:")
    print("-" * W)
    if not trades:
        print("  Sin trades completados en el período.")
    for t in trades:
        icon  = "+ WIN " if t["pnl"] > 0 else ("- LOSS" if t["pnl"] < 0 else "= BE  ")
        open_ = " (posicion abierta)" if "*" in t["out_t"] else ""
        sign  = "+" if t["pnl"] >= 0 else ""
        print(f"  #{t['n']}  {icon}  {t['side']:<5}  {t['in_t']}->{t['out_t']}  "
              f"${t['in_p']:.2f}->${t['out_p']:.2f}  {sign}{t['pnl']:.2f}%{open_}")
        if t.get("note"): print(f"         [{t['note']}]")

    print()

    # ── Resumen estadístico ────────────────────────────────────────────────────
    print("RESUMEN:")
    print("-" * W)
    if not trades:
        print("  Sin trades para resumir.")
        print()
        return

    closed  = [t for t in trades if "*" not in t["out_t"]]
    all_pnl = [t["pnl"] for t in trades]
    wins    = [p for p in all_pnl if p > 0]
    losses  = [p for p in all_pnl if p < 0]
    total   = sum(all_pnl)
    n       = len(trades)

    print(f"  Trades totales:    {n}  ({len(closed)} cerrados, {n-len(closed)} abierto)")
    print(f"  Ganadores:         {len(wins)} ({len(wins)/n*100:.0f}%)")
    print(f"  Perdedores:        {len(losses)} ({len(losses)/n*100:.0f}%)")
    print(f"  P&L total:         {'+' if total>=0 else ''}{total:.2f}%")
    print(f"  P&L promedio:      {'+' if total/n>=0 else ''}{total/n:.2f}%/trade")
    if wins:   print(f"  Máx ganancia:      +{max(wins):.2f}%")
    if losses: print(f"  Máx pérdida:       {min(losses):.2f}%")

    # Barras totales con contexto
    rng_start = lima(bars1[30]) if bars1 else "?"
    rng_end   = lima(bars1[-1]) if bars1 else "?"
    print(f"  Periodo analizado: {rng_start} - {rng_end} Lima")
    print(f"  Barras totales:    {len(bars1)}  |  Barras con senal: {len(bars1)-30}")
    if time_stopped:
        print(f"  Time-stops:        {len(time_stopped)} (a las {', '.join(time_stopped)})")
    print()
    print("MEJORAS v3 ACTIVAS:")
    print(f"  [1] Time-stop:     cierre automatico si >{MAX_POSITION_SCANS} scans con -{ADVERSE_MOVE_PCT}%+ adverso")
    print(f"  [2] CERRAR umbral: opposite score >=3 (antes >=4)")
    print(f"  [3] RSI override:  SHORT se cierra si RSI<30 / LONG si RSI>70")
    print(f"  [4] Momentum adj:  patrones debiles penalizados si precio +/-0.5% en 10 bars")
    print(f"  [5] SIP threshold: entrada fuera de horario regular requiere {SIP_ENTRY_THRESH}/9 (regular=5/9)")
    print()


if __name__ == "__main__":
    run()
