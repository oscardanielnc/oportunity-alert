"""
Valida cobertura de earnings de Finnhub (free tier) sobre el universo mega-cap de PED.
Consulta /calendar/earnings en una ventana pasada y reporta qué mega-caps aparecen
con fecha. Uso: python -m research.finnhub_earnings_coverage
"""
import os
import requests
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

FINNHUB_BASE = "https://finnhub.io/api/v1"
DAYS_BACK = 100

MEGA_CAP = [
    "NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "TSM", "ORCL", "AMD", "NFLX", "CRM", "ADBE", "COST",
    "WMT", "JPM", "V", "MA", "UNH", "XOM", "CVX", "LLY", "HD",
]


def main():
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        raise SystemExit("ERROR: FINNHUB_API_KEY no esta en el entorno")

    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    resp = requests.get(
        f"{FINNHUB_BASE}/calendar/earnings",
        params={"from": date_from, "to": date_to, "token": key},
        timeout=30,
    )
    print(f"HTTP {resp.status_code} | ventana {date_from} -> {date_to}")
    resp.raise_for_status()
    data = resp.json()
    cal = data.get("earningsCalendar", [])
    print(f"Total entradas en calendario: {len(cal)}")

    # Mapa symbol -> fecha mas reciente reportada
    seen = {}
    for e in cal:
        sym = (e.get("symbol") or "").upper()
        d = e.get("date") or ""
        if sym and d:
            if sym not in seen or d > seen[sym]:
                seen[sym] = d

    print(f"\nCobertura mega-cap PED (ultimos {DAYS_BACK} dias):")
    print(f"{'TICKER':<8} {'REPORTO?':<10} FECHA")
    print("-" * 40)
    cubiertos = 0
    faltan = []
    for t in MEGA_CAP:
        if t in seen:
            cubiertos += 1
            print(f"{t:<8} {'SI':<10} {seen[t]}")
        else:
            faltan.append(t)
            print(f"{t:<8} {'NO':<10} -")
    print("-" * 40)
    print(f"Cobertura: {cubiertos}/{len(MEGA_CAP)} mega-caps con earnings en la ventana")
    if faltan:
        print(f"Sin earnings en ventana (puede ser normal si ya reporto antes): {faltan}")


if __name__ == "__main__":
    main()
