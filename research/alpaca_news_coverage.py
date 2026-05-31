"""
Prueba descartable: ¿el feed de noticias de Alpaca (Benzinga) cubre la watchlist?
Consulta el endpoint REST de noticias (histórico reciente) por ticker y reporta:
  - cuántos artículos trajo en los últimos N días
  - la fuente (confirmar = Benzinga)
  - un titular de muestra + antigüedad

Uso:  python -m research.alpaca_news_coverage
"""
import os
import sys
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

import requests

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

WATCHLIST = [
    "NVDA", "TSM", "QBTS", "IONQ", "RGTI", "QUBT", "PLTR", "APP",
    "AVGO", "AMD", "ASML", "ARM", "RDDT", "CRM", "MSFT", "GOOG",
    "CCJ", "TLN", "BE", "EME", "NOC", "RTX", "LMT", "KTOS", "AVAV",
    "UBER", "SHOP", "MU", "COHR", "IBM", "XOM", "CVX",
]

DAYS_BACK = 14


def _headers():
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        sys.exit("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY no estan en el entorno (.env)")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def fetch_news(headers, symbol, start_iso):
    params = {
        "symbols": symbol,
        "start": start_iso,
        "limit": 50,
        "sort": "desc",
    }
    r = requests.get(NEWS_URL, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("news", [])


def main():
    headers = _headers()
    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Cobertura Alpaca News (Benzinga) - ultimos {DAYS_BACK} dias")
    print(f"{'TICKER':<8}{'#ART':>5}  {'FUENTE':<12} ULTIMO TITULAR")
    print("-" * 90)

    sources = set()
    sin_cobertura = []
    for sym in WATCHLIST:
        try:
            items = fetch_news(headers, sym, start_iso)
        except requests.HTTPError as e:
            print(f"{sym:<8}{'ERR':>5}  HTTP {e.response.status_code if e.response else '?'}")
            continue
        except Exception as e:
            print(f"{sym:<8}{'ERR':>5}  {e}")
            continue

        n = len(items)
        if n == 0:
            sin_cobertura.append(sym)
            print(f"{sym:<8}{0:>5}  {'-':<12} (sin noticias en la ventana)")
            continue

        top = items[0]
        src = top.get("source", "?")
        sources.add(src)
        created = top.get("created_at", "")
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_h = (now - dt).total_seconds() / 3600
            age = f"hace {age_h:.0f}h"
        except Exception:
            age = created
        headline = (top.get("headline", "") or "")[:48]
        print(f"{sym:<8}{n:>5}  {src:<12} [{age}] {headline}")

    print("-" * 90)
    print(f"Fuentes vistas: {sources}")
    cubiertos = len(WATCHLIST) - len(sin_cobertura)
    print(f"Cobertura: {cubiertos}/{len(WATCHLIST)} tickers con >=1 articulo en {DAYS_BACK} dias")
    if sin_cobertura:
        print(f"Sin noticias en ventana (puede ser solo baja actividad): {sin_cobertura}")


if __name__ == "__main__":
    main()
