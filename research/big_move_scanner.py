#!/usr/bin/env python3
"""
ESTUDIO INVERSO — desde el RESULTADO hacia la causa.

En vez de partir de nuestras alertas, barre el universo y detecta TODOS los saltos
anormales grandes (>= UMBRAL% anormal vs QQQ, dentro de <= 4h, intradía), y para cada uno:
  - mide el onset (minuto en que arranca el grueso del movimiento) y el pico,
  - lo cruza con nuestras alertas (alerts_vm.csv) para saber si lo vimos y con cuánto
    LEAD TIME (alerta antes del onset = a tiempo; después = tarde),
  - le pega la categoría real de catalizador (alerts_meta clasificado).

Responde: ¿qué catalizador produce los +X%? ¿llegamos a tiempo o tarde?

Barras 1m de Alpaca SIP (free), CACHEADAS a disco (data/bars_cache/) -> no re-descarga.
READ-ONLY sobre el sistema vivo.

Uso:
  python research/big_move_scanner.py                 # umbral 5%, ventana de los datos
  python research/big_move_scanner.py --thr 5 --refresh TICKER
"""
from __future__ import annotations
import os, sys, csv, time, argparse, bisect
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for _l in (Path(_ROOT) / ".env").read_text().splitlines() if (Path(_ROOT) / ".env").exists() else []:
    _l = _l.strip()
    if "=" in _l and not _l.startswith("#"):
        k, v = _l.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

import requests
import pandas as pd
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ALPACA_BASE = "https://data.alpaca.markets"
CACHE_DIR = os.path.join(_ROOT, "data", "bars_cache")
ALERTS = os.path.join(_ROOT, "data", "alerts_vm.csv")
META_CLASS = os.path.join(_ROOT, "data", "catalyst_reactions.csv")  # id->categoria (de classify_catalysts)
OUT = os.path.join(_ROOT, "data", "big_moves_catalog.csv")

PERIOD_START = datetime(2026, 5, 15, tzinfo=timezone.utc)
WINDOW_MIN = 240           # movimiento debe completarse en <= 4h
BENCH = "QQQ"
EXCLUDE = {"XRP"}          # cripto / no-stock en Alpaca


# ───────────────────────── fetch + cache ─────────────────────────
def _headers():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def fetch_alpaca(symbol, start, end):
    params = {"timeframe": "1Min",
              "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "feed": "sip", "limit": 10000, "sort": "asc"}
    rows = []
    url = f"{ALPACA_BASE}/v2/stocks/{symbol}/bars"
    while True:
        r = requests.get(url, headers=_headers(), params=params, timeout=60)
        if r.status_code != 200:
            print(f"  [WARN] {symbol} {r.status_code}: {r.text[:80]}")
            break
        d = r.json()
        for b in (d.get("bars") or []):
            rows.append((b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))
        tok = d.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"])
    if not df.empty:
        df["ts"] = pd.to_datetime(df["t"], utc=True)
        df = df.drop(columns="t").set_index("ts").sort_index()
    return df


def get_bars(symbol, end, refresh=False) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{symbol}_1m.parquet")
    if os.path.exists(path) and not refresh:
        return pd.read_parquet(path)
    df = fetch_alpaca(symbol, PERIOD_START, end)
    if not df.empty:
        df.to_parquet(path)
    return df


# ───────────────────────── detección de saltos ─────────────────────────
def et_date(ts: pd.Timestamp):
    return (ts - timedelta(hours=4)).date()    # ET = UTC-4 (EDT)


def detect_moves(df: pd.DataFrame, bench: pd.DataFrame, thr: float):
    """Por día ET, ancla en el primer bar y busca la mayor corrida anormal trough->peak
    (>= thr%, <= WINDOW_MIN) en ambas direcciones. Devuelve lista de eventos."""
    if df.empty or bench.empty:
        return []
    # alinear bench a los timestamps de la acción (último <= t)
    bench = bench[["c"]].rename(columns={"c": "bc"})
    j = df.join(bench, how="left")
    j["bc"] = j["bc"].ffill()
    j = j.dropna(subset=["bc"])
    events = []
    for d, g in j.groupby([et_date(t) for t in j.index]):
        if len(g) < 10:
            continue
        p0, q0 = g["c"].iloc[0], g["bc"].iloc[0]
        abn = ((g["c"] / p0) - (g["bc"] / q0)) * 100.0   # serie anormal intradía (%)
        ts = list(g.index)
        vals = list(abn.values)
        ev = _largest_run(ts, vals, thr, +1) or None
        evd = _largest_run(ts, vals, thr, -1) or None
        for e in (ev, evd):
            if e:
                e["date"] = str(d)
                events.append(e)
    return events


def _largest_run(ts, vals, thr, sign):
    """Mayor corrida trough->peak (sign +1) o peak->trough (sign -1) en <= WINDOW_MIN.
    O(n^2) acotado por día (~960 bars máx). Devuelve dict o None si < thr."""
    n = len(vals)
    best = None
    # para +1: buscamos j>i con vals[j]-vals[i] máx y vals[i] es mínimo previo
    # implementación: mínimo (o máximo) corriente con restricción de ventana temporal
    anchor_i = 0
    for j in range(1, n):
        # avanzar anchor para mantener ventana <= WINDOW_MIN y ser el mejor extremo
        while (ts[j] - ts[anchor_i]).total_seconds() / 60 > WINDOW_MIN:
            anchor_i += 1
        # extremo dentro de [anchor_i, j]
        seg = vals[anchor_i:j + 1]
        if sign > 0:
            base = min(seg); move = vals[j] - base
            bi = anchor_i + seg.index(base)
        else:
            base = max(seg); move = base - vals[j]
            bi = anchor_i + seg.index(base)
        if move >= thr and (best is None or move > best["magnitude"]):
            best = {"onset": ts[bi], "peak": ts[j],
                    "magnitude": round(move, 2),
                    "ttp_min": int((ts[j] - ts[bi]).total_seconds() // 60),
                    "direccion": "UP" if sign > 0 else "DOWN"}
    return best


# ───────────────────────── cruce con alertas ─────────────────────────
def load_alerts():
    out = []
    for r in csv.DictReader(open(ALERTS, encoding="utf-8")):
        try:
            lima = datetime.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        out.append({"id": r["id"], "ticker": r["ticker"],
                    "ts": (lima + timedelta(hours=5)).replace(tzinfo=timezone.utc),
                    "direccion": r["direccion"]})
    return out


def load_categories():
    if not os.path.exists(META_CLASS):
        return {}
    return {r["id"]: r["categoria"] for r in csv.DictReader(open(META_CLASS, encoding="utf-8"))}


def match_alert(ev, ticker, alerts, cats):
    """Alerta más cercana del ticker en [onset-3h, peak]. lead_min>0 => alerta ANTES del onset."""
    onset, peak = ev["onset"], ev["peak"]
    cand = [a for a in alerts if a["ticker"] == ticker
            and onset - timedelta(hours=3) <= a["ts"] <= peak]
    if not cand:
        return None
    a = min(cand, key=lambda x: abs((x["ts"] - onset).total_seconds()))
    return {"alert_id": a["id"], "alert_ts": a["ts"].strftime("%Y-%m-%dT%H:%MZ"),
            "lead_min": int((onset - a["ts"]).total_seconds() // 60),
            "categoria": cats.get(a["id"], "?"), "dir_ia": a["direccion"]}


# ─────────────────────────── main ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thr", type=float, default=5.0, help="umbral %% anormal")
    ap.add_argument("--refresh", default="", help="ticker(s) a re-descargar (coma) o ALL")
    args = ap.parse_args()

    universe = sorted(set(a["ticker"] for a in load_alerts()) - EXCLUDE)
    # añadir watchlist faltante (los 17) — se lee del propio union ya en alerts? no, agregamos manual
    extra = ["CCJ", "CVX", "EME", "GOOG", "IBM", "IONQ", "LMT", "MSFT", "NOC",
             "QUBT", "RTX", "TSM", "UBER", "XOM"]
    universe = sorted(set(universe) | set(extra) - EXCLUDE)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    refresh = set(universe) if args.refresh == "ALL" else set(args.refresh.split(",")) if args.refresh else set()

    print(f"Universo: {len(universe)} tickers | umbral {args.thr}% anormal en <= {WINDOW_MIN}min")
    bench = get_bars(BENCH, end, BENCH in refresh)
    print(f"Bench {BENCH}: {len(bench)} barras")

    alerts = load_alerts()
    cats = load_categories()

    catalog = []
    t0 = time.time()
    for i, tk in enumerate(universe, 1):
        try:
            df = get_bars(tk, end, tk in refresh)
        except Exception as e:
            print(f"  [{i}/{len(universe)}] {tk}: ERROR {str(e)[:60]}"); continue
        if df.empty:
            continue
        evs = detect_moves(df, bench, args.thr)
        for e in evs:
            m = match_alert(e, tk, alerts, cats)
            catalog.append({"ticker": tk, "date": e["date"], "direccion": e["direccion"],
                            "magnitude": e["magnitude"], "ttp_min": e["ttp_min"],
                            "onset": e["onset"].strftime("%Y-%m-%dT%H:%MZ"),
                            "peak": e["peak"].strftime("%Y-%m-%dT%H:%MZ"),
                            "alerta": "SI" if m else "NO",
                            "lead_min": m["lead_min"] if m else None,
                            "categoria": m["categoria"] if m else None,
                            "dir_ia": m["dir_ia"] if m else None,
                            "alert_ts": m["alert_ts"] if m else None})
    print(f"Scan listo en {time.time()-t0:.0f}s")

    catalog.sort(key=lambda x: -x["magnitude"])
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(catalog[0].keys())); w.writeheader(); w.writerows(catalog)
    _report(catalog, args.thr)
    print(f"\nCatalogo -> {OUT}")


def _report(cat, thr):
    n = len(cat)
    up = [c for c in cat if c["direccion"] == "UP"]
    seen = [c for c in cat if c["alerta"] == "SI"]
    print(f"\n=== {n} saltos >= {thr}% anormal (<=4h) | UP {len(up)} / DOWN {n-len(up)} ===")
    print(f"Con alerta nuestra: {len(seen)}/{n} ({100*len(seen)//max(n,1)}%) | SIN alerta (perdidos): {n-len(seen)}")

    leads = [c["lead_min"] for c in seen if c["lead_min"] is not None]
    if leads:
        ontime = sum(1 for l in leads if l > 0)
        leads.sort()
        print(f"\n=== LEAD TIME (alerta vs onset del movimiento) — n={len(leads)} ===")
        print(f"  A TIEMPO (alerta ANTES del onset): {ontime}/{len(leads)} ({100*ontime//len(leads)}%)")
        print(f"  TARDE (alerta DESPUES del onset):  {len(leads)-ontime}/{len(leads)}")
        print(f"  lead_min mediana: {leads[len(leads)//2]:+d} min  (rango {leads[0]:+d}..{leads[-1]:+d})")

    print(f"\n=== Categoría de los saltos CON alerta ===")
    from collections import Counter
    cc = Counter(c["categoria"] for c in seen)
    for k, v in cc.most_common():
        print(f"  {k or '?':<18} {v}")

    print(f"\n=== TOP 15 saltos (el catálogo de oro) ===")
    print(f"{'ticker':<6} {'date':<11} {'dir':<4} {'mag%':>6} {'ttp':>4} {'alerta':>6} {'lead':>6} {'categoria':<16}")
    for c in cat[:15]:
        print(f"{c['ticker']:<6} {c['date']:<11} {c['direccion']:<4} {c['magnitude']:>6} "
              f"{c['ttp_min']:>4} {c['alerta']:>6} {str(c['lead_min']) if c['lead_min'] is not None else '—':>6} "
              f"{(c['categoria'] or '—'):<16}")


if __name__ == "__main__":
    main()
