#!/usr/bin/env python3
"""
Mide el impacto de los posts RECIENTES de Trump (mirror RSS, últimos ~100) que nombran una
empresa concreta. Desde el minuto del POST mide la reacción en Binance perp (24/7 → captura
overnight, donde Trump suele postear) y en Alpaca si es horario de mercado.

Responde: cuando Trump posteó (p.ej. Apple/Intel), ¿la acción empezó a moverse? ¿en qué dirección?
READ-ONLY. Uso: python research/trump_rss_impact.py
"""
from __future__ import annotations
import os, sys, re, bisect, itertools
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
for _l in open(os.path.join(_ROOT, ".env")):
    _l = _l.strip()
    if "=" in _l and not _l.startswith("#"):
        k, v = _l.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

import ccxt
RSS = "https://trumpstruth.org/feed"
# nombre en el post -> ticker (empresas con perp en Binance para medir 24/7)
NAME2TK = {"intel": "INTC", "apple": "AAPL", "nvidia": "NVDA", "tesla": "TSLA", "amd": "AMD",
           "broadcom": "AVGO", "micron": "MU", "boeing": "BA", "microsoft": "MSFT",
           "meta": "META", "amazon": "AMZN", "google": "GOOGL", "qualcomm": "QCOM"}
_HTML = re.compile(r"<[^>]+>")
_ex = ccxt.binanceusdm({"enableRateLimit": True}); _ex.load_markets()
_cache = {}


def perp_bars(tk, start, end):
    sym = f"{tk}/USDT:USDT"
    if sym not in _ex.markets:
        return None
    if tk in _cache:
        return _cache[tk]
    since = int(start.timestamp() * 1000); endms = int(end.timestamp() * 1000); out = []
    while since < endms:
        o = _ex.fetch_ohlcv(sym, "1m", since=since, limit=1000)
        if not o:
            break
        out += o; since = o[-1][0] + 1
        if len(o) < 1000:
            break
    _cache[tk] = ([b[0] // 1000 for b in out], [b[4] for b in out])
    return _cache[tk]


def at(series, when):
    ts, cl = series
    i = bisect.bisect_right(ts, int(when.timestamp())) - 1
    return cl[i] if i >= 0 and int(when.timestamp()) - ts[i] < 90 * 60 else None


def main():
    r = requests.get(RSS, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    items = ET.fromstring(r.text).findall(".//item")
    now = datetime.now(timezone.utc)
    rows = []
    seen = set()
    for it in items:
        text = _HTML.sub(" ", (it.findtext("description") or it.findtext("title") or "")).strip()
        tl = text.lower()
        tk = next((NAME2TK[n] for n in NAME2TK if re.search(r"\b" + n + r"\b", tl)), None)
        if not tk:
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            continue
        key = (tk, pub.strftime("%Y%m%d%H"))
        if key in seen:
            continue
        seen.add(key)
        rows.append((pub, tk, text[:75]))

    print(f"Posts recientes que nombran una empresa medible: {len(rows)}\n")
    print(f"{'fecha (UTC)':<17}{'tk':<6}{'+15m':>7}{'+1h':>7}{'+4h':>7}{'+24h':>8}  titular")
    for pub, tk, txt in sorted(rows):
        b = perp_bars(tk, pub - timedelta(minutes=30), min(now, pub + timedelta(hours=26)))
        if not b or not b[0]:
            print(f"{pub.strftime('%m-%d %H:%M'):<17}{tk:<6}{'sin perp/datos':>29}  {txt}"); continue
        p0 = at(b, pub)
        def ret(h):
            p = at(b, pub + timedelta(hours=h))
            return f"{(p/p0-1)*100:+.1f}%" if (p and p0) else "—"
        print(f"{pub.strftime('%m-%d %H:%M'):<17}{tk:<6}"
              f"{ret(.25):>7}{ret(1):>7}{ret(4):>7}{ret(24):>8}  {txt}")
    print("\n(retorno RAW del perp desde el minuto del post; overnight no tiene benchmark QQQ)")


if __name__ == "__main__":
    main()
