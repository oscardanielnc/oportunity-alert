#!/usr/bin/env python3
"""
Calendario de PRÓXIMOS earnings — base del pre-posicionamiento y de la vista de la sección.

`utils/earnings_calendar.py` solo mira hacia ATRÁS (¿reportó hoy/ayer?). Esto mira hacia
ADELANTE: ¿quién reporta en los próximos N días? Fuente: Finnhub /calendar/earnings (free tier),
filtrando filas FUTURAS (sin epsActual todavía: aún no reportan) con fecha en [hoy, hoy+N].

Uso:
    from earnings.calendar import upcoming_earnings
    upcoming_earnings(tickers, days_ahead=14) -> [{ticker, date, hour, days_until}]
"""
import os
import time
import requests
from datetime import datetime, timezone, timedelta

FINNHUB_BASE = "https://finnhub.io/api/v1"
_cache: dict = {}   # (ticker, hoy) -> [eventos futuros]


def upcoming_earnings(tickers, days_ahead=14):
    """
    Earnings AGENDADOS (aún no reportados) en [hoy, hoy+days_ahead] para `tickers`.
    Retorna lista ordenada por fecha: [{ticker, date 'YYYY-MM-DD', hour 'amc'|'bmo', days_until}].
    Falla silenciosamente a [] por ticker si Finnhub no responde.
    """
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    today = datetime.now(timezone.utc).date()
    date_to = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    out = []
    for tk in tickers:
        ck = (tk, today_s)
        if ck in _cache:
            out.extend(_cache[ck]); continue
        evs = []
        try:
            r = requests.get(
                f"{FINNHUB_BASE}/calendar/earnings",
                params={"from": today_s, "to": date_to, "symbol": tk, "token": key},
                timeout=15,
            )
            if r.status_code == 200:
                for e in r.json().get("earningsCalendar", []):
                    d = e.get("date") or ""
                    if not (today_s <= d <= date_to):
                        continue
                    # FUTURO = aún no reportado (epsActual/revenueActual vacíos). Si ya tiene
                    # actual, es de hoy ya reportado -> no es "próximo".
                    if e.get("epsActual") is not None or e.get("revenueActual") is not None:
                        continue
                    hour = (e.get("hour") or "").lower()
                    if hour not in ("amc", "bmo"):
                        hour = "amc"
                    du = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
                    evs.append({"ticker": tk, "date": d, "hour": hour, "days_until": du,
                                "eps_estimate": e.get("epsEstimate"),
                                "rev_estimate": e.get("revenueEstimate")})
        except Exception:
            pass
        _cache[ck] = evs
        out.extend(evs)
        time.sleep(0.05)
    out.sort(key=lambda x: (x["date"], x["ticker"]))
    return out
