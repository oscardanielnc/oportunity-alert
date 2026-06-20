"""
Polling de noticias via Finnhub API.
Cubre upgrades de analistas, earnings, noticias generales por ticker.
"""
import os
import logging
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Sub-fuentes de Finnhub company-news que se DESCARTAN al ingestar (validado backfill 2026-06-20):
# laggy + bajo rendimiento. Yahoo: age mediana 106min, 3% llega a IA, 3 alertas en 25 días.
# CNBC: ~473min de lag. El Benzinga real-time entra por el WebSocket de Alpaca (age 0), no acá.
# El backbone de velocidad = ALPACA_BENZINGA (WS) + SEC_EDGAR. SeekingAlpha/Fintel se conservan
# (traen catalizadores únicos aunque laggeados). Ajustable sin tocar lógica.
DEPRIORITIZED_FINNHUB_SOURCES = {"YAHOO", "CNBC"}


def _get_api_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        raise EnvironmentError("FINNHUB_API_KEY debe estar en el entorno")
    return key


def fetch_company_news(ticker: str, hours_back: int = 1) -> list[dict]:
    """
    Retorna noticias recientes para un ticker específico.
    """
    try:
        api_key = _get_api_key()
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(hours=hours_back)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        resp = requests.get(
            f"{FINNHUB_BASE}/company-news",
            params={
                "symbol": ticker,
                "from": date_from,
                "to": date_to,
                "token": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()

        articles = []
        for item in items:
            try:
                pub_ts = item.get("datetime", 0)
                if pub_ts:
                    pub_dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
                else:
                    pub_dt = now
                age_minutes = (now - pub_dt).total_seconds() / 60

                source = item.get("source", "Finnhub")
                # Descartar sub-fuentes laggeadas/deprior­izadas (Yahoo/CNBC) — ver constante arriba.
                if source.upper() in DEPRIORITIZED_FINNHUB_SOURCES:
                    continue

                headline = item.get("headline", "")
                summary = item.get("summary", "")
                url = item.get("url", "")
                item_id = item.get("id", "")
                if not item_id:
                    item_id = hashlib.md5(url.encode()).hexdigest()

                articles.append({
                    "id": str(item_id),
                    "source": f"FINNHUB_{source.upper()}",
                    "title": headline,
                    "summary": summary,
                    "url": url,
                    "published_at": pub_dt.isoformat(),
                    "age_minutes": round(age_minutes, 1),
                    "tickers_found": [ticker],
                    "raw_text": f"{headline} {summary}".upper(),
                })
            except Exception as e:
                logger.warning(f"Error procesando item Finnhub {ticker}: {e}")
                continue

        return articles

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            logger.warning(f"Finnhub rate limit alcanzado para {ticker}")
        else:
            logger.error(f"HTTP error Finnhub {ticker}: {e}")
        return []
    except Exception as e:
        logger.error(f"Error Finnhub news {ticker}: {e}")
        return []


def fetch_market_news(category: str = "general", hours_back: int = 1) -> list[dict]:
    """
    Retorna noticias generales del mercado (no por ticker).
    Útil para detectar noticias macro que afecten múltiples tickers.
    """
    try:
        api_key = _get_api_key()
        now = datetime.now(timezone.utc)

        resp = requests.get(
            f"{FINNHUB_BASE}/news",
            params={"category": category, "token": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()

        articles = []
        for item in items[:20]:  # limitar a 20 noticias generales
            try:
                pub_ts = item.get("datetime", 0)
                pub_dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc) if pub_ts else now
                age_minutes = (now - pub_dt).total_seconds() / 60

                if age_minutes > 60:
                    continue  # solo noticias de la última hora

                headline = item.get("headline", "")
                summary = item.get("summary", "")
                url = item.get("url", "")
                item_id = item.get("id", hashlib.md5(url.encode()).hexdigest())

                articles.append({
                    "id": str(item_id),
                    "source": "FINNHUB_MARKET",
                    "title": headline,
                    "summary": summary,
                    "url": url,
                    "published_at": pub_dt.isoformat(),
                    "age_minutes": round(age_minutes, 1),
                    "tickers_found": [],  # se determinan en el filtro
                    "raw_text": f"{headline} {summary}".upper(),
                })
            except Exception:
                continue

        return articles

    except Exception as e:
        logger.error(f"Error Finnhub market news: {e}")
        return []
