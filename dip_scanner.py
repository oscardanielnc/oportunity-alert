"""
Dip Scanner — escaneo diario de soportes de entrada sobre TODO el universo.

Universo = watchlist canónica (metrics.db) + posiciones eToro abiertas (read-only),
deduplicado. Por cada ticker calcula soportes corto-plazo + estructurales y chips de
riesgo (utils/dip_levels), y escribe data/dip_dashboard.json ordenado por el % al
soporte corto más cercano (ascendente: arriba = más cerca de entrada ideal).

Sección 100% INFORMATIVA: no envía alertas ni ejecuta nada. Oscar revisa el card,
verifica si la caída es macro (sana) o idiosincrática (riesgo) y decide.

Uso:
    python dip_scanner.py            # corre el escaneo y escribe el JSON
    python dip_scanner.py --quiet    # sin tabla en consola

Pensado para correr 1 vez/día tras el cierre (cron en la VM), como earnings/scoreboard.
"""
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from utils.dip_levels import fetch_daily_bars, spy_return_5d, analyze_ticker

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))
OUT_PATH = Path(__file__).parent / "data" / "dip_dashboard.json"


def _universe() -> dict:
    """{ticker: live_price|None}. Watchlist (metrics.db) + posiciones eToro abiertas."""
    universe = {}

    # Watchlist canónica
    try:
        from utils.metrics_store import MetricsStore
        for row in MetricsStore().get_watchlist():
            t = (row.get("ticker") or "").upper().strip()
            if t:
                universe.setdefault(t, None)
    except Exception as e:
        logger.warning(f"[Dips] No se pudo leer watchlist: {e}")

    # Posiciones eToro abiertas (con precio en vivo)
    try:
        from utils.etoro_client import get_portfolio
        pf = get_portfolio()
        for p in pf.get("positions", []):
            t = (p.get("ticker") or "").upper().strip()
            if t:
                universe[t] = p.get("current_rate") or None
    except Exception as e:
        logger.warning(f"[Dips] No se pudo leer eToro: {e}")

    return universe


def run(quiet: bool = False) -> dict:
    universe = _universe()
    if not universe:
        print("[Dips] Universo vacío — ¿watchlist/eToro disponibles?")
        return {"available": False, "cards": []}

    spy_ret5 = spy_return_5d()
    cards = []
    errors = []

    for ticker, live in universe.items():
        try:
            bars = fetch_daily_bars(ticker, limit=300)
            res = analyze_ticker(ticker, bars, spy_ret5=spy_ret5, live_price=live)
            if res and res.get("nearest_pct") is not None:
                cards.append(res)
            elif not res:
                errors.append(ticker)
            time.sleep(0.15)   # cortesía con Alpaca
        except Exception as e:
            logger.warning(f"[Dips] {ticker}: {e}")
            errors.append(ticker)

    # Ranking: más cerca del soporte corto = arriba
    cards.sort(key=lambda c: c["nearest_pct"])

    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "as_of": datetime.now(LIMA).strftime("%Y-%m-%d %H:%M"),
        "spy_5d": spy_ret5,
        "count": len(cards),
        "skipped": errors,
        "cards": cards,
    }

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    if not quiet:
        _print_console(data)
    print(f"[Dips] {len(cards)} tickers escritos en {OUT_PATH.name}"
          + (f" ({len(errors)} sin datos: {', '.join(errors[:8])})" if errors else ""))
    return data


def _ascii(s: str) -> str:
    """Windows cp1252-safe: la consola puede no soportar tildes/unicode de los labels."""
    return s.encode("ascii", "replace").decode("ascii")


def _print_console(data: dict):
    """Resumen ASCII (Windows cp1252-safe: sin emojis ni unicode)."""
    print(f"\nDIP SCANNER - {data['as_of']} Lima  (SPY 5d: {data['spy_5d']:+.1f}%)")
    print("-" * 78)
    print(f"{'#':>2} {'TICKER':<7}{'PRECIO':>10}{'SOP.CORTO':>22}{'%':>7}  RIESGO")
    print("-" * 78)
    for i, c in enumerate(data["cards"][:20], 1):
        sc = c["short_supports"][0] if c["short_supports"] else None
        sop = f"{_ascii(sc['label'])[:14]} {sc['price']}" if sc else "-"
        pct = f"+{sc['dist_pct']:.1f}%" if sc else "-"
        r = c["risk"]
        flags = []
        flags.append("TEND-ok" if r["trend_healthy"] else "TEND-riesgo")
        flags.append(f"RSI{int(r['rsi'])}")
        flags.append(f"DD{r['drawdown']:.0f}%")
        if r["idiosyncratic"]:
            flags.append(f"IDIO{r['vs_spy']:+.0f}")
        if r["vol_ratio"] >= 1.8:
            flags.append(f"VOLx{r['vol_ratio']:.1f}")
        print(f"{i:>2} {c['ticker']:<7}{c['price']:>10}{sop:>22}{pct:>7}  {' '.join(flags)}")
    print("-" * 78)


if __name__ == "__main__":
    run(quiet="--quiet" in sys.argv)
