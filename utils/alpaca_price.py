"""
Precio real-time via Alpaca Markets API.
Cubre pre-market (4am ET) y after-hours (hasta 8pm ET).
Usa feed=iex (gratuito, incluye AH).
"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

ALPACA_BASE = "https://data.alpaca.markets"
ALPACA_FEED = "iex"  # gratuito, incluye pre-market y AH

# Tickers que Alpaca IEX no cubre (crypto, ETFs inversos, etc.)
# Para estos se devuelve precio sin error para no bloquear el pipeline
ALPACA_UNSUPPORTED = {"BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "ADA"}


def _get_headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise EnvironmentError("ALPACA_API_KEY y ALPACA_SECRET_KEY deben estar en el entorno")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def get_realtime_price(ticker: str) -> dict:
    """
    Retorna precio actual del ticker incluyendo pre-market y AH.

    Retorna:
        {
            "ticker": str,
            "current_price": float,
            "prev_close": float,
            "change_pct": float,
            "last_trade_time": str (ISO),
            "last_trade_age_minutes": float,
            "candles_1m": list[dict],   # últimas 10 velas de 1 min
            "volume_ratio": float,       # volumen actual vs promedio 20d (estimado)
            "error": str | None
        }
    """
    result = {
        "ticker": ticker,
        "current_price": None,
        "prev_close": None,
        "change_pct": None,
        "last_trade_time": None,
        "last_trade_age_minutes": None,
        "candles_1m": [],
        "volume_ratio": None,
        "error": None,
    }

    if ticker in ALPACA_UNSUPPORTED:
        result["error"] = f"{ticker} es crypto — precio via Binance, no Alpaca"
        logger.debug(f"Alpaca no soporta {ticker} (crypto) — continuando sin precio")
        return result

    try:
        headers = _get_headers()

        # Último trade
        trade_resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/trades/latest",
            params={"feed": ALPACA_FEED},
            headers=headers,
            timeout=10,
        )
        trade_resp.raise_for_status()
        trade_data = trade_resp.json().get("trade", {})
        current_price = float(trade_data.get("p", 0))
        trade_time_str = trade_data.get("t", "")

        result["current_price"] = current_price
        result["last_trade_time"] = trade_time_str

        # Calcular antigüedad del último trade
        if trade_time_str:
            try:
                trade_dt = dateparser.parse(trade_time_str)
                now_utc = datetime.now(timezone.utc)
                if trade_dt.tzinfo is None:
                    trade_dt = trade_dt.replace(tzinfo=timezone.utc)
                age_minutes = (now_utc - trade_dt).total_seconds() / 60
                result["last_trade_age_minutes"] = round(age_minutes, 1)
            except Exception:
                pass

        # Snapshot para prev_close y change_pct
        snap_resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/snapshot",
            params={"feed": ALPACA_FEED},
            headers=headers,
            timeout=10,
        )
        if snap_resp.status_code == 200:
            snap = snap_resp.json()
            prev_close = snap.get("prevDailyBar", {}).get("c")
            if prev_close and current_price:
                result["prev_close"] = float(prev_close)
                result["change_pct"] = round(
                    (current_price - float(prev_close)) / float(prev_close) * 100, 2
                )

        # Velas de 1 minuto — últimas 10 (últimas 2h para asegurar)
        now_utc = datetime.now(timezone.utc)
        start = (now_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars_resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
            params={
                "timeframe": "1Min",
                "start": start,
                "feed": ALPACA_FEED,
                "limit": 10,
                "sort": "desc",
            },
            headers=headers,
            timeout=10,
        )
        if bars_resp.status_code == 200:
            bars = bars_resp.json().get("bars", [])
            result["candles_1m"] = [
                {
                    "t": b.get("t"),
                    "o": b.get("o"),
                    "h": b.get("h"),
                    "l": b.get("l"),
                    "c": b.get("c"),
                    "v": b.get("v"),
                }
                for b in bars
            ]

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            result["error"] = f"Ticker {ticker} no encontrado en Alpaca"
        else:
            result["error"] = f"HTTP error Alpaca: {e}"
        logger.warning(result["error"])
    except Exception as e:
        result["error"] = f"Error obteniendo precio Alpaca: {e}"
        logger.error(result["error"])

    return result


def summarize_candles(candles: list) -> str:
    """Genera resumen legible de velas para el prompt de Claude."""
    if not candles:
        return "Sin datos de velas"
    try:
        # Las velas vienen en orden desc (más reciente primero)
        recent = candles[:5]
        greens = sum(1 for c in recent if c.get("c", 0) >= c.get("o", 0))
        reds = len(recent) - greens
        vols = [c.get("v", 0) for c in recent if c.get("v")]
        vol_trend = "vol creciente" if len(vols) >= 2 and vols[0] > vols[-1] else "vol decreciente"
        return f"{greens} velas verdes / {reds} rojas en últimas 5 velas 1m, {vol_trend}"
    except Exception:
        return "Error resumiendo velas"
