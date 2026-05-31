#!/usr/bin/env python3
"""
PED — ¿A QUÉ HORA del D+2 entrar?  (estudio INTRADÍA con velas de 1 minuto)

Confirmado ya: PED entra en D+2 (no antes). Pregunta de Oscar (2026-05-31): ¿a qué HORA
del D+2? Su hipótesis: al abrir hay avalancha de compradores que dispara el precio los
primeros minutos → entrar al open sería comprar en la cima; mejor esperar un pullback de
unos minutos, o comprar 5-10 min ANTES de la apertura. ¿O las acciones BAJAN al abrir?

Esto NO se ve en barras diarias — se necesita 1-minuto del D+2. Para cada evento PED que
califica (mega-cap, reacción +5..+25%, sin gap-down >5%) se bajan las velas de 1 min del
D+2 y se mide:
  1) CAMINO del precio relativo al OPEN (09:30 ET): pre-market, +5/+15/+30/+60 min, cierre.
     ¿Sube (pop) o baja (dip) en los primeros minutos?
  2) RETORNO en P&L desde cada punto de entrada hasta la MISMA salida (Day+7 close):
     premkt / open / +5 / +15 / +30 / +60 min / cierre D+2. ¿Qué hora da el mejor retorno?

Comisión idéntica en todas (no cambia el ranking) → se compara retorno gross.
Timestamps Alpaca en UTC → convertidos a America/New_York (maneja DST). feed=SIP.
"""
import os, sys, json, time, statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_ped import UNIVERSE, fetch_earnings_events, WEEKS
from pilot.ped_signals import PED_THRESHOLD, PED_PRICED_CAP, PED_GAP_FLOOR

NY = ZoneInfo("America/New_York")
OPEN_MIN = 9*60 + 30          # 09:30 ET en minutos del día
CLOSE_MIN = 16*60            # 16:00 ET
OFFSETS = [5, 15, 30, 60]    # minutos tras el open a medir
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ped_intraday_cache.json")


def _et(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(NY)


def _px_at(session, m):
    """Cierre de la vela en el offset >= m más cercano (None si no hay sesión)."""
    if not session:
        return None
    for o in sorted(session):
        if o >= m:
            return session[o]["c"]
    return session[max(session)]["c"]


def build_event_prices(tk, d2_date, exit_px):
    """Baja 1-min del D+2 y arma los precios de entrada intradía + retorno a exit_px."""
    start = f"{d2_date}T11:00:00Z"; end = f"{d2_date}T21:30:00Z"   # cubre premkt..cierre (DST-safe)
    bars = fetch_all(tk, "1Min", start, end=end, limit=3000)
    session = {}; premkt = []
    for b in bars:
        et = _et(b["t"])
        if et.strftime("%Y-%m-%d") != d2_date:
            continue
        mins = et.hour*60 + et.minute
        off = mins - OPEN_MIN
        if -40 <= off < 0:
            premkt.append(b["c"])
        elif 0 <= off <= (CLOSE_MIN - OPEN_MIN):
            session[off] = b
    if 0 not in session and not session:
        return None
    open_px = session[0]["o"] if 0 in session else session[min(session)]["o"]
    if not open_px or open_px <= 0:
        return None
    entries = {
        "premkt": premkt[-1] if premkt else None,     # ~09:25 (5-10 min antes del open)
        "open":   open_px,
        "close":  session[max(session)]["c"],
    }
    for m in OFFSETS:
        entries[f"+{m}m"] = _px_at(session, m)
    # retorno de cada entrada hasta la salida común (Day+7 close)
    rets = {k: ((exit_px - p)/p*100 if p and p > 0 else None) for k, p in entries.items()}
    # camino relativo al open (¿pop o dip en los primeros minutos?)
    path = {k: ((entries[k] - open_px)/open_px*100 if entries[k] else None)
            for k in ["premkt", "+5m", "+15m", "+30m", "+60m", "close"]}
    return {"entries": entries, "rets": rets, "path": path}


def extract():
    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    out = []
    for tk in UNIVERSE:
        daily = fetch_all(tk, "1Day", start_iso, limit=2000)
        if len(daily) < 30:
            print(f"  {tk:<6} sin diarios"); continue
        daily = [{"t": b["t"][:10], "o": float(b["o"]), "h": float(b["h"]),
                  "l": float(b["l"]), "c": float(b["c"])} for b in daily]
        dates = [b["t"] for b in daily]; idx = {d: i for i, d in enumerate(dates)}
        events = fetch_earnings_events(tk, sd, today)
        n = 0
        for ev in events:
            E, h = ev["date"], ev["hour"]
            if E not in idx:
                continue
            ei = idx[E]; r = ei if h == "bmo" else ei + 1
            if r - 1 < 0 or r + 6 >= len(daily):
                continue
            pre = daily[r-1]["c"]
            if pre <= 0:
                continue
            reac = (daily[r]["c"] - pre)/pre*100
            gap = (daily[r]["o"] - pre)/pre*100
            if not (PED_THRESHOLD <= reac <= PED_PRICED_CAP and gap >= PED_GAP_FLOOR):
                continue
            d2_date = daily[r+1]["t"]; exit_px = daily[r+6]["c"]
            res = build_event_prices(tk, d2_date, exit_px)
            time.sleep(0.12)
            if not res:
                continue
            res.update({"ticker": tk, "d2_date": d2_date, "reac": round(reac, 1)})
            out.append(res); n += 1
        print(f"  {tk:<6} {n} eventos PED con intradía")
    return out


def _agg(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return {"n": len(v), "mean": statistics.mean(v), "med": statistics.median(v),
            "pos": sum(1 for x in v if x > 0)/len(v)*100}


def main():
    print(f"\n{'#'*92}")
    print(f"  PED — TIMING INTRADÍA del D+2  |  {len(UNIVERSE)} large-caps | velas 1-min")
    print(f"{'#'*92}")
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        evs = json.load(open(CACHE, encoding="utf-8"))
        print(f"  (cache: {len(evs)} eventos — usar --fresh para recomputar)")
    else:
        evs = extract()
        json.dump(evs, open(CACHE, "w", encoding="utf-8"))
    print(f"\n  Eventos PED con intradía del D+2: {len(evs)}")

    # ── 1) CAMINO del precio relativo al OPEN (¿pop o dip los primeros minutos?) ──
    print(f"\n{'='*92}")
    print(f"  1) CAMINO DEL PRECIO vs OPEN (09:30 ET)  — ¿sube (pop) o baja (dip) tras abrir?")
    print(f"{'='*92}")
    print(f"  {'Punto':<10} {'n':>4} {'media%':>9} {'mediana%':>10} {'%veces>open':>12}")
    print(f"  {'-'*48}")
    for k, lab in [("premkt", "premkt(~9:25)"), ("+5m", "open+5m"), ("+15m", "open+15m"),
                   ("+30m", "open+30m"), ("+60m", "open+60m"), ("close", "cierre D+2")]:
        s = _agg([e["path"].get(k) for e in evs])
        if s:
            print(f"  {lab:<10} {s['n']:>4} {s['mean']:>+8.2f}% {s['med']:>+9.2f}% {s['pos']:>11.0f}%")
    print(f"\n  premkt media>0 => el open suele estar POR ENCIMA del premarket (comprar antes = más barato).")
    print(f"  +5m/+15m media<0 => baja tras abrir (dip): esperar unos min entra MÁS barato (tu pullback).")
    print(f"  +5m/+15m media>0 => pop al abrir: entrar al open o antes; esperar te encarece.")

    # ── 2) RETORNO hasta salida común (Day+7) por hora de entrada en D+2 ─────────
    print(f"\n{'='*92}")
    print(f"  2) RETORNO HASTA Day+7 close POR HORA DE ENTRADA EN D+2 (gross, comisión igual)")
    print(f"{'='*92}")
    print(f"  {'Entrada':<14} {'n':>4} {'media%':>9} {'mediana%':>10} {'WR%':>6} {'vs open':>9}")
    print(f"  {'-'*56}")
    ref = _agg([e["rets"].get("open") for e in evs])
    for k, lab in [("premkt", "premkt(~9:25)"), ("open", "open (9:30)"), ("+5m", "open+5m"),
                   ("+15m", "open+15m"), ("+30m", "open+30m"), ("+60m", "open+60m"),
                   ("close", "cierre D+2")]:
        s = _agg([e["rets"].get(k) for e in evs])
        if s:
            d = s["mean"] - ref["mean"]
            print(f"  {lab:<14} {s['n']:>4} {s['mean']:>+8.2f}% {s['med']:>+9.2f}% {s['pos']:>5.0f}% {d:>+8.2f}%")
    print(f"\n  'vs open' = cuánto MEJOR/peor que entrar al open (9:30), la referencia D+2 actual.")
    print(f"  La hora con mayor media% Y buen WR, sin más riesgo, es la mejor entrada intradía.\n")


if __name__ == "__main__":
    main()
