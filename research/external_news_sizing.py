#!/usr/bin/env python3
"""
DIMENSIONA cubeta 2 (había noticia que NO trajimos) vs cubeta 3 (momentum sin noticia)
para los saltos >=12% que salieron NO_VISTO y NO son earnings.

Para cada (ticker, día del salto) consulta Finnhub company-news (gratis) y busca un titular
de CATALIZADOR real cerca del onset [onset-8h, onset+1h]. Clasifica:
  - CUBETA_2_NEWS   : había titular de catalizador cerca del onset -> hueco de FUENTE (arreglable)
  - CUBETA_3_MOMENTUM: no había titular de catalizador (solo recaps "stock moving" o nada)

Cachea las respuestas en data/finnhub_news_cache/. READ-ONLY.
Uso: python research/external_news_sizing.py
"""
from __future__ import annotations
import os, sys, csv, json, time, requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
for _l in (Path(_ROOT) / ".env").read_text().splitlines():
    _l = _l.strip()
    if "=" in _l and not _l.startswith("#"):
        k, v = _l.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

KEY = os.environ["FINNHUB_API_KEY"]
CACHE = os.path.join(_ROOT, "data", "finnhub_news_cache")
MISSES = os.path.join(_ROOT, "data", "misses_12.csv")
AUTOPSY = os.path.join(_ROOT, "data", "autopsy_misses.csv")

# earnings detectados antes (cubeta 1, se excluyen aquí)
EARNINGS = {("APPS", "2026-05-26"), ("APPS", "2026-05-27"), ("DELL", "2026-05-28"),
            ("DY", "2026-05-27"), ("GME", "2026-06-02"), ("MRVL", "2026-05-27"),
            ("SNOW", "2026-05-27"), ("ZS", "2026-05-26")}

# titular = CATALIZADOR real (causa), no recap del movimiento (efecto)
CATALYST = ["prospectus", "offering", "dilut", "registered direct", "convertible", "shares of common",
            "contract", "award", "deal", "partnership", "collaborat", "agreement", "memorandum",
            "launch", "unveil", "fda", "approval", "phase ", "clinical", "trial",
            "upgrade", "downgrade", "price target", "initiates", "rating",
            "stake", "13d", "13g", "lawsuit", "investigation", "short seller", "short report",
            "acquire", "acquisition", "merger", "buyout", "guidance", "resigns", "steps down",
            "ceo", "appoints", "wins", "selected", "secures", "raises", "files for",
            "bankrupt", "delist", "going concern", "accuses", "fraud", "subpoena", "patent"]
# recaps / ruido (NO son causa)
RECAP = ["stocks moving", "whale activity", "intraday session", "stocks to watch", "movers",
         "why is", "stocks that", "market clubhouse", "biggest movers", "top stocks",
         "stocks setting", "premarket", "midday", "session: gainers", "trading higher", "trading lower"]


def fetch_news(ticker, d0, d1):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{ticker}_{d0}_{d1}.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    r = requests.get("https://finnhub.io/api/v1/company-news",
                     params={"symbol": ticker, "from": d0, "to": d1, "token": KEY}, timeout=25)
    data = r.json() if r.status_code == 200 else []
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    time.sleep(0.25)
    return data


def is_catalyst(h):
    hl = h.lower()
    if any(r in hl for r in RECAP):
        return False
    return any(c in hl for c in CATALYST)


def main():
    auto = {(r["ticker"], r["date"]): r for r in csv.DictReader(open(AUTOPSY, encoding="utf-8"))}
    misses = list(csv.DictReader(open(MISSES, encoding="utf-8")))
    # NO_VISTO y no earnings
    target = [m for m in misses
              if auto.get((m["ticker"], m["date"]), {}).get("verdict") == "NO_VISTO"
              and (m["ticker"], m["date"]) not in EARNINGS]
    print(f"NO_VISTO no-earnings a evaluar: {len(target)}\n")

    rows = []
    for m in target:
        onset = datetime.strptime(m["onset"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        d = onset.date()
        news = fetch_news(m["ticker"], str(d - timedelta(days=1)), str(d))
        lo, hi = onset - timedelta(hours=8), onset + timedelta(hours=1)
        near = []
        for n in news:
            ndt = datetime.fromtimestamp(n["datetime"], timezone.utc)
            if lo <= ndt <= hi and is_catalyst(n["headline"]):
                near.append((ndt, n.get("source", ""), n["headline"]))
        near.sort()
        bucket = "CUBETA_2_NEWS" if near else "CUBETA_3_MOMENTUM"
        best = near[0] if near else None
        rows.append({"ticker": m["ticker"], "date": m["date"], "direccion": m["direccion"],
                     "magnitude": m["magnitude"], "onset": m["onset"], "bucket": bucket,
                     "n_catalyst_news": len(near),
                     "lead_min": int((onset - best[0]).total_seconds() // 60) if best else None,
                     "src": best[1] if best else None,
                     "headline": best[2][:90] if best else None})

    from collections import Counter
    bc = Counter(r["bucket"] for r in rows)
    print("=== RESULTADO ===")
    for b, n in bc.most_common():
        print(f"  {b:<20} {n}  ({100*n//len(rows)}%)")

    print("\n=== CUBETA 2 — noticia/catalizador que NO trajimos (lo capturable con buena fuente) ===")
    c2 = [r for r in rows if r["bucket"] == "CUBETA_2_NEWS"]
    c2.sort(key=lambda r: -float(r["magnitude"]))
    for r in c2:
        lead = f"+{r['lead_min']}m antes" if r["lead_min"] and r["lead_min"] > 0 else f"{r['lead_min']}m"
        print(f"  {r['ticker']:<5} {r['date']} {r['direccion']:<4} {r['magnitude']:>5}%  "
              f"[{r['src'][:9]:<9} {lead}]  \"{r['headline']}\"")

    print(f"\n=== CUBETA 3 — sin titular de catalizador (momentum) — por ticker ===")
    c3 = [r for r in rows if r["bucket"] == "CUBETA_3_MOMENTUM"]
    for t, n in Counter(r["ticker"] for r in c3).most_common():
        print(f"  {t:<6} {n}")

    out = os.path.join(_ROOT, "data", "external_news_sizing.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
