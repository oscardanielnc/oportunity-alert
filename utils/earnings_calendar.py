"""
earnings_calendar.py — ¿el ticker reporta/reportó earnings hoy o ayer?

Rescatado del premarket scanner (archivado 2026-05-30). Hallazgo VALIDADO con datos:
entrar al pop intradía en día de earnings es net-negativo (−9.9% promedio en backtest).
La jugada correcta de earnings NO es el pop intradía sino el PED (Post-Earnings Drift)
multi-día en mega-caps con reacción fuerte — ver research/backtest_ped.py.

Uso en Noticias: marcar/avisar cuando una alerta de catalizador cae en día de earnings.
"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
LIMA = timezone(timedelta(hours=-5))

_cache: dict = {}  # {ticker_fecha: bool} — evita repetir llamadas el mismo día


def has_earnings_today_or_yesterday(ticker: str) -> bool:
    """
    True si el ticker reportó earnings hoy o ayer (gap ya priceado).
    Cacheado por día. Falla a False si Finnhub no responde.
    """
    today = datetime.now(LIMA).strftime("%Y-%m-%d")
    ck = f"{ticker}_{today}"
    if ck in _cache:
        return _cache[ck]
    res = False
    try:
        key = os.environ.get("FINNHUB_API_KEY", "")
        if key:
            yest = (datetime.now(LIMA) - timedelta(days=1)).strftime("%Y-%m-%d")
            r = requests.get(
                "https://finnhub.io/api/v1/calendar/earnings",
                params={"from": yest, "to": today, "symbol": ticker, "token": key},
                timeout=10,
            )
            if r.status_code == 200:
                res = bool(r.json().get("earningsCalendar"))
    except Exception as e:
        logger.debug(f"[earnings] check {ticker}: {e}")
    _cache[ck] = res
    return res
