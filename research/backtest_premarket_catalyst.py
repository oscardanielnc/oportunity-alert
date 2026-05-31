#!/usr/bin/env python3
"""
FASE B — ¿el edge del pre-market existe MÁS ALLÁ de MU?

Para cada señal pre-market (large-cap, momentum sostenido):
  1. Tag earnings (Finnhub calendar) — ya validado que earnings = fade.
  2. Tag CATALIZADOR clasificando las noticias del ticker ese día (Finnhub company-news).
  3. Mide P&L neto-de-fees POR TIPO DE CATALIZADOR → la data dice cuáles son "tradeables".
  4. Repite TODO sin MU → ¿se sostiene el edge sin el ticker que dominaba?

Reusa: super_backtest.fetch_all, backtest_perticker_v2.etoro_fee_analysis,
backtest_premarket.build_trades + fetch_earnings_blackout.
Salida same-day. LONG only (shorts no viables). Sin spread/slippage.
"""
import os, sys, time, re
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_perticker_v2 import etoro_fee_analysis
from backtest_premarket import build_trades, fetch_earnings_blackout

WEEKS = 20

# Universo large-cap 24/5 ampliado (más tickers = menos dependencia de MU)
UNIVERSE = [
    "NVDA", "AMD", "MU", "AVGO", "MSFT", "PLTR", "GOOG", "CRM", "ARM", "UBER",
    "SHOP", "TSM", "COHR", "AAPL", "META", "AMZN", "NFLX", "ORCL", "ADBE", "QCOM",
    "INTC", "MRVL", "DELL", "ANET", "SMCI", "NOW", "PANW", "CRWD",
]

# ── Clasificación de catalizador por keywords (consistente con keyword_filter) ──
CATALYST_RULES = [
    ("upgrade",     ["RAISES PRICE TARGET", "PRICE TARGET", "RAISES TARGET", "UPGRADE",
                     "UPGRADED", "OVERWEIGHT", "OUTPERFORM", "BUY RATING", "INITIATED",
                     "RAISED TO BUY", "STREET-HIGH", "BULLISH"]),
    ("contract_gov",["CONTRACT", "AWARD", "AWARDED", "CHIPS ACT", "DEFENSE", "PENTAGON",
                     "GOVERNMENT", "DOD ", "FEDERAL", "BILLION DEAL"]),
    ("m&a",         ["ACQUIRE", "ACQUISITION", "MERGER", "BUYOUT", "TAKEOVER", "TENDER OFFER"]),
    ("fda",         ["FDA", "APPROVAL", "PHASE 3", "PHASE 2", "PDUFA", "CLEARANCE"]),
    ("product_ai",  ["LAUNCH", "UNVEIL", "PARTNERSHIP", "DEAL WITH", "SUPPLY AGREEMENT",
                     "CHIP", "GPU", "AI ", "DATA CENTER", "ORDER"]),
    ("guidance",    ["GUIDANCE", "RAISES FORECAST", "RAISES OUTLOOK", "PRELIMINARY"]),
    ("earnings",    ["EARNINGS", "EPS", "REVENUE", "QUARTERLY", "BEATS", "RESULTS", "Q1",
                     "Q2", "Q3", "Q4", "PROFIT"]),
]


def _finnhub_news(ticker, day):
    """Noticias del ticker en [day-2, day] — el catalizador del movimiento premarket."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    try:
        d_to = day
        d_from = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        r = requests.get("https://finnhub.io/api/v1/company-news",
                         params={"symbol": ticker, "from": d_from, "to": d_to, "token": key},
                         timeout=12)
        if r.status_code != 200:
            return []
        return [f"{it.get('headline','')} {it.get('summary','')}".upper()
                for it in (r.json() or [])][:12]
    except Exception:
        return []


def classify_catalyst(ticker, day, is_earnings):
    if is_earnings:
        return "earnings"
    texts = _finnhub_news(ticker, day)
    blob = " ".join(texts)
    if not blob.strip():
        return "sin_catalizador"
    for cat, kws in CATALYST_RULES:
        if any(kw in blob for kw in kws):
            return cat
    return "otro_news"


def _net2k(trades):
    longs = [t for t in trades if t["side"] == "LONG"]
    if not longs:
        return None
    fa = etoro_fee_analysis(longs)["long"]
    return {
        "n": fa["n"],
        "gross": fa["gross_avg_pnl"],
        "wr": fa["gross_wr_pct"],
        "net2k": fa["net_by_position_size"]["2000"]["net_avg_pnl"],
        "net5k": fa["net_by_position_size"]["5000"]["net_avg_pnl"],
    }


def _line(label, trades):
    s = _net2k(trades)
    if not s:
        print(f"  {label:<34} (sin LONG)")
        return
    flag = "[+]" if s["net2k"] > 0 else "[-]"
    print(f"  {label:<34} n={s['n']:>3}  gross {s['gross']:>+7.3f}%  WR {s['wr']:>3}%  "
          f"NET@2k {s['net2k']:>+7.3f}% {flag}")


def run():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd, ed = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"\n{'#'*94}\n  FASE B — catalizador + ¿edge sin MU?  | {WEEKS} sem | {len(UNIVERSE)} large-caps\n{'#'*94}")

    import json
    CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_phaseb_cache.json")
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        all_tr = json.load(open(CACHE, encoding="utf-8"))
        print(f"  (cache: {len(all_tr)} senales — usar --fresh para recomputar)")
    else:
        all_tr = []
        for tk in UNIVERSE:
            bars = fetch_all(tk, "5Min", start, limit=20000)
            if not bars:
                print(f"  {tk:<6} sin datos"); continue
            trades = [t for t in build_trades(tk, bars) if t["side"] == "LONG"]
            if not trades:
                print(f"  {tk:<6} 0 senales"); continue
            bl = fetch_earnings_blackout(tk, sd, ed)
            for t in trades:
                t["is_earnings"] = t["date"] in bl
                t["catalyst"] = classify_catalyst(tk, t["date"], t["is_earnings"])
                time.sleep(0.15)  # Finnhub rate limit
            all_tr += trades
            cats = {}
            for t in trades:
                cats[t["catalyst"]] = cats.get(t["catalyst"], 0) + 1
            print(f"  {tk:<6} {len(trades):>2} senales | {cats}")
        json.dump(all_tr, open(CACHE, "w", encoding="utf-8"))

    # ── Por tipo de catalizador ──
    print(f"\n{'='*94}\n  P&L NETO POR TIPO DE CATALIZADOR (la data decide qué es 'tradeable')\n{'='*94}")
    types = sorted({t["catalyst"] for t in all_tr})
    for ct in types:
        _line(ct, [t for t in all_tr if t["catalyst"] == ct])

    # ── Filtros acumulados ──
    print(f"\n{'='*94}\n  FILTROS ACUMULADOS\n{'='*94}")
    no_earn = [t for t in all_tr if not t["is_earnings"]]
    # "tradeable" provisional = catalizador fundamental (no earnings, no sin_catalizador)
    tradeable_cats = {"upgrade", "contract_gov", "m&a", "fda", "product_ai", "guidance"}
    cat_ok = [t for t in no_earn if t["catalyst"] in tradeable_cats]
    _line("TODO", all_tr)
    _line("sin earnings", no_earn)
    _line("sin earnings + catalizador fundamental", cat_ok)

    # ── ¿Se sostiene SIN MU? ──
    print(f"\n{'='*94}\n  ¿EDGE SIN MU?  (quitar MU del cálculo)\n{'='*94}")
    nomu = [t for t in all_tr if t["ticker"] != "MU"]
    nomu_ne = [t for t in nomu if not t["is_earnings"]]
    nomu_cat = [t for t in nomu_ne if t["catalyst"] in tradeable_cats]
    _line("sin MU — TODO", nomu)
    _line("sin MU — sin earnings", nomu_ne)
    _line("sin MU — sin earnings + cataliz fundamental", nomu_cat)

    print(f"\n  Lectura: [+] = net-positivo @2k. Si 'sin MU + cataliz fundamental' es [+] con n decente,")
    print(f"           el madrugón tiene edge real y replicable. Si solo MU sostiene, se archiva.")
    print(f"  (Muestra aún limitada; sin spread/slippage. Clasificación por keywords de noticias.)\n")


if __name__ == "__main__":
    run()
