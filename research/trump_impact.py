#!/usr/bin/env python3
"""
Mide el impacto REAL de eventos Trump con la maquinaria de reacción (retorno anormal vs QQQ,
MFE/MAE) sobre barras 1m cacheadas. Cruza trump_feed_vm.json + casos conocidos.

El campo `tickers` del tracker viene None -> se extrae el ticker del headline por mapa de nombres.
READ-ONLY. Uso: python research/trump_impact.py
"""
from __future__ import annotations
import os, sys, json, bisect
from datetime import datetime, timedelta, timezone
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
CACHE = os.path.join(_ROOT, "data", "bars_cache")

NAME2TK = {"intel": "INTC", "asml": "ASML", "nvidia": "NVDA", "tesla": "TSLA",
           "rigetti": "RGTI", "d-wave": "QBTS", "ionq": "IONQ", "micron": "MU",
           "marvell": "MRVL", "broadcom": "AVGO", "amd": "AMD"}

_cache = {}
def bars(sym):
    if sym in _cache:
        return _cache[sym]
    p = os.path.join(CACHE, f"{sym}_1m.parquet")
    if not os.path.exists(p):
        _cache[sym] = None; return None
    df = pd.read_parquet(p)
    t = [int(x.timestamp()) for x in df.index]
    _cache[sym] = (t, df["c"].tolist()); return _cache[sym]

def price_at(sym, when):
    c = bars(sym)
    if not c:
        return None
    ts = int(when.timestamp()); i = bisect.bisect_right(c[0], ts) - 1
    if i < 0 or ts - c[0][i] > 90 * 60:
        return None
    return c[1][i]

def abn(sym, t0, mins):
    p0, p1 = price_at(sym, t0), price_at(sym, t0 + timedelta(minutes=mins))
    q0, q1 = price_at("QQQ", t0), price_at("QQQ", t0 + timedelta(minutes=mins))
    if None in (p0, p1, q0, q1) or not p0 or not q0:
        return None
    return round((p1/p0 - 1)*100 - (q1/q0 - 1)*100, 2)

def mfe_mae(sym, t0):
    c = bars(sym); p0 = price_at(sym, t0); q0 = price_at("QQQ", t0)
    if not c or not p0 or not q0:
        return None, None
    t0s = int(t0.timestamp()); end = t0s + 24*3600
    i = bisect.bisect_right(c[0], t0s); mfe = mae = 0.0
    while i < len(c[0]) and c[0][i] <= end:
        q = price_at("QQQ", datetime.fromtimestamp(c[0][i], timezone.utc))
        if q:
            a = ((c[1][i]/p0) - (q/q0))*100
            mfe = max(mfe, a); mae = min(mae, a)
        i += 1
    return round(mfe, 2), round(mae, 2)

def extract_tk(headline):
    h = headline.lower()
    for name, tk in NAME2TK.items():
        if name in h:
            return tk
    return None

def measure(label, tk, ts_lima, direction):
    t0 = datetime.strptime(ts_lima[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(hours=5)
    if bars(tk) is None:
        print(f"  {label:<42} {tk}: sin barras en cache"); return
    a1, a4, a24 = abn(tk, t0, 60), abn(tk, t0, 240), abn(tk, t0, 1440)
    mfe, mae = mfe_mae(tk, t0)
    ok = ""
    if a4 is not None and direction in ("alcista", "bajista"):
        up = a4 > 0
        ok = "✓" if (up and direction == "alcista") or (not up and direction == "bajista") else "✗"
    print(f"  {label:<42} {tk:<5} [{direction:<8}] abn1h={str(a1):>6} abn4h={str(a4):>6} "
          f"abn24h={str(a24):>6} MFE={str(mfe):>5} MAE={str(mae):>6} {ok}")


def main():
    print("=== Eventos Trump del feed (jun 18-19) ===")
    items = json.load(open(os.path.join(_ROOT, "data", "trump_feed_vm.json"), encoding="utf-8"))
    items = items.get("items", items) if isinstance(items, dict) else items
    seen = set()
    for it in items:
        tk = extract_tk(it["headline"])
        if not tk:
            print(f"  (sin ticker mapeable) {it['headline'][:55]}"); continue
        key = (tk, it["ts"][:13])
        if key in seen:
            continue
        seen.add(key)
        measure(it["headline"][:42], tk, it["ts"], it.get("impacto", "?"))

    print("\n=== Casos conocidos de alto impacto (manual) ===")
    measure("Trump Admin take Quantum Stakes", "RGTI", "2026-05-21T09:00:00", "alcista")
    measure("Trump Admin take Quantum Stakes", "QBTS", "2026-05-21T09:00:00", "alcista")
    measure("Trump Admin take Quantum Stakes", "IONQ", "2026-05-21T09:00:00", "alcista")

    print("\nLectura: abn = retorno anormal vs QQQ; MFE/MAE = excursión máx en 24h; ✓/✗ = acertó la dirección.")


if __name__ == "__main__":
    main()
