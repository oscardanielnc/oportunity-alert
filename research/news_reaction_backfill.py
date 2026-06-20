#!/usr/bin/env python3
"""
FASE 0 — Backfill histórico de reacción a noticias (estudio de evento).

Lee las alertas YA capturadas (CSV exportado de la VM, o metrics_vm.db) y reconstruye, con
barras 1m históricas de Alpaca, cuánto y qué tan rápido se movió la acción TRAS la noticia,
NETO del mercado y del sector (retorno anormal). Produce tablas de calibración score->reacción
y tipo->reacción, + el run-up pre-t0 (cuán tarde llegó nuestra alerta).

READ-ONLY: no toca metrics.db de runtime ni el sistema vivo. Es research.

Metodología (por qué Alpaca para todo en el backfill): el retorno anormal exige acción y
benchmark (QQQ/sector) en el MISMO reloj de sesión. Por eso el stock se mide con Alpaca SIP
(cubre pre/after market). Se marca aparte qué tickers tienen perp Binance y qué alertas caen
fuera de sesión (data_gap), insumo para la fase forward 24/7.

Uso:
  python research/news_reaction_backfill.py --csv data/alerts_vm.csv
  python research/news_reaction_backfill.py --csv data/alerts_vm.csv --limit 20 --verbose
"""
from __future__ import annotations

import os
import sys
import csv as csvmod
import time
import bisect
import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# cargar .env (Alpaca creds) sin depender de python-dotenv
for _line in (Path(_ROOT) / ".env").read_text().splitlines() if (Path(_ROOT) / ".env").exists() else []:
    _line = _line.strip()
    if "=" in _line and not _line.startswith("#"):
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

import requests  # noqa: E402

try:
    import ccxt  # noqa: E402
except Exception:
    ccxt = None

from pilot.star_score import SECTOR_ETF, DEFAULT_ETF  # noqa: E402

# ───────────────────────── CONFIG ─────────────────────────
ALPACA_BASE = "https://data.alpaca.markets"
ALPACA_FEED = "sip"

CHECKPOINTS_MIN = [15, 30, 60, 120, 240, 24 * 60]   # +15m .. +4h, +24h
WINDOW_PRE_MIN = 120          # t0-2h para run-up pre-alerta
WINDOW_POST_MIN = 48 * 60     # tope 48h
PERP_HISTORY_DAYS = 45

OUT_CSV = os.path.join(_ROOT, "data", "news_reactions_backfill.csv")

# ──────────────────── disponibilidad Binance (solo flag) ────────────────────
_binance_markets = None

def binance_has_perp(ticker: str) -> bool:
    global _binance_markets
    if ccxt is None:
        return False
    if _binance_markets is None:
        try:
            _binance_markets = ccxt.binanceusdm({"enableRateLimit": True}).load_markets()
        except Exception as e:
            print(f"[WARN] no se pudo cargar markets Binance: {e}")
            _binance_markets = {}
    return f"{ticker}/USDT:USDT" in _binance_markets


# ──────────────────── barras: fetch + cache por símbolo ────────────────────
# Cache: symbol -> (times[epoch_s asc], closes, highs, lows)
_bars_cache: dict[str, tuple] = {}

def _alpaca_fetch_range(symbol: str, start: datetime, end: datetime) -> list[dict]:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise EnvironmentError("ALPACA_API_KEY/ALPACA_SECRET_KEY no están en el entorno")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "timeframe": "1Min",
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feed": ALPACA_FEED, "limit": 10000, "sort": "asc",
    }
    out = []
    url = f"{ALPACA_BASE}/v2/stocks/{symbol}/bars"
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code != 200:
            print(f"[WARN] Alpaca {symbol} {r.status_code}: {r.text[:100]}")
            break
        data = r.json()
        out.extend(data.get("bars") or [])
        tok = data.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    return out


def load_symbol(symbol: str, start: datetime, end: datetime):
    """Baja TODO el rango una vez y lo cachea como arrays ordenados (rápido vía bisect)."""
    if symbol in _bars_cache:
        return _bars_cache[symbol]
    bars = _alpaca_fetch_range(symbol, start, end)
    times, closes, highs, lows = [], [], [], []
    for b in bars:
        t = datetime.strptime(b["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        times.append(int(t.timestamp())); closes.append(b["c"]); highs.append(b["h"]); lows.append(b["l"])
    _bars_cache[symbol] = (times, closes, highs, lows)
    return _bars_cache[symbol]


def price_at(symbol: str, when: datetime):
    """Cierre de la última barra con t <= when. None si no hay barra previa en cache."""
    cache = _bars_cache.get(symbol)
    if not cache or not cache[0]:
        return None
    times, closes = cache[0], cache[1]
    ts = int(when.timestamp())
    i = bisect.bisect_right(times, ts) - 1
    if i < 0:
        return None
    # exigir que la barra previa esté razonablemente cerca (<90 min) -> si no, hay hueco de sesión
    if ts - times[i] > 90 * 60:
        return None
    return closes[i]


def ret_pct(symbol: str, a: datetime, b: datetime):
    pa, pb = price_at(symbol, a), price_at(symbol, b)
    if pa is None or pb is None or not pa:
        return None
    return (pb / pa - 1.0) * 100.0


# ─────────────────────── cálculo de reacción ───────────────────────
def compute_reaction(alert: dict, etf: str) -> dict:
    tkr = alert["ticker"]
    t0 = alert["t0"]
    p0 = price_at(tkr, t0)
    if p0 is None:
        return {"status": "data_gap", "data_gap": True}

    row = {"price_t0": round(p0, 4), "data_gap": False, "status": "closed"}

    # run-up pre-t0 (cuán tarde llegamos): anormal vs QQQ
    for lbl, mins in (("pre2h", WINDOW_PRE_MIN), ("pre30m", 30)):
        rs = ret_pct(tkr, t0 - timedelta(minutes=mins), t0)
        rq = ret_pct("QQQ", t0 - timedelta(minutes=mins), t0)
        row[f"runup_{lbl}_abn"] = None if rs is None or rq is None else round(rs - rq, 3)

    # checkpoints forward
    for mins in CHECKPOINTS_MIN:
        when = t0 + timedelta(minutes=mins)
        rs = ret_pct(tkr, t0, when)
        rq = ret_pct("QQQ", t0, when)
        rsec = ret_pct(etf, t0, when)
        tag = f"{mins}m"
        row[f"ret_{tag}"] = None if rs is None else round(rs, 3)
        row[f"abn_{tag}"] = None if rs is None or rq is None else round(rs - rq, 3)
        row[f"abnsec_{tag}"] = None if rs is None or rsec is None else round(rs - rsec, 3)

    # MFE/MAE anormal (vs QQQ rebased a t0) sobre la ventana post; tiempo al pico
    mfe, mae, tpeak = _mfe_mae(tkr, t0, p0)
    row.update({"mfe_abn": mfe, "mae_abn": mae, "time_to_peak_min": tpeak})

    # acierto direccional: signo de la reacción a +4h vs predicción IA
    abn4 = row.get("abn_240m")
    d = (alert.get("direccion") or "").upper()
    if abn4 is not None and d in ("LONG", "SHORT"):
        up = abn4 > 0
        row["direccion_correcta"] = (up and d == "LONG") or (not up and d == "SHORT")
    else:
        row["direccion_correcta"] = None
    return row


def _mfe_mae(tkr: str, t0: datetime, p0: float):
    cache = _bars_cache.get(tkr)
    q0 = price_at("QQQ", t0)
    if not cache or not cache[0] or not q0 or not p0:
        return None, None, None
    times, closes = cache[0], cache[1]
    t0s = int(t0.timestamp()); end_s = t0s + WINDOW_POST_MIN * 60
    i = bisect.bisect_right(times, t0s)
    mfe, mae, tpeak = 0.0, 0.0, None
    while i < len(times) and times[i] <= end_s:
        when = datetime.fromtimestamp(times[i], timezone.utc)
        q = price_at("QQQ", when)
        if q:
            abn = ((closes[i] / p0) - (q / q0)) * 100.0
            if abn > mfe:
                mfe, tpeak = abn, (times[i] - t0s) // 60
            if abn < mae:
                mae = abn
        i += 1
    return round(mfe, 3), round(mae, 3), tpeak


# ─────────────────────── carga de alertas ───────────────────────
def _to_utc(ts_lima: str) -> datetime | None:
    try:
        lima = datetime.strptime(ts_lima[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        try:
            lima = datetime.strptime(ts_lima[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return (lima + timedelta(hours=5)).replace(tzinfo=timezone.utc)  # Lima(UTC-5) -> UTC


def load_alerts_csv(path: str, limit: int | None = None) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csvmod.DictReader(f):
            t0 = _to_utc(r["ts"])
            if not t0 or not r.get("ticker"):
                continue
            rows.append({
                "id": r["id"], "ticker": r["ticker"], "t0": t0,
                "source": r.get("source"), "tipo_catalizador": r.get("tipo_catalizador"),
                "direccion": r.get("direccion"),
                "pct_estimado": _f(r.get("pct_estimado")),
                "score": _i(r.get("conviction_score")),
                "horizonte": r.get("horizonte_tiempo"),
            })
    rows.sort(key=lambda x: x["t0"])
    return rows[:limit] if limit else rows


def load_alerts_db(path: str, limit: int | None = None) -> list[dict]:
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row
    q = ("SELECT id, ts, ticker, source, tipo_catalizador, direccion, pct_estimado, "
         "conviction_score, horizonte_tiempo FROM alerts WHERE ticker IS NOT NULL ORDER BY ts")
    rows = []
    for r in con.execute(q):
        t0 = _to_utc(r["ts"])
        if not t0:
            continue
        rows.append({"id": r["id"], "ticker": r["ticker"], "t0": t0, "source": r["source"],
                     "tipo_catalizador": r["tipo_catalizador"], "direccion": r["direccion"],
                     "pct_estimado": r["pct_estimado"], "score": r["conviction_score"],
                     "horizonte": r["horizonte_tiempo"]})
    con.close()
    return rows[:limit] if limit else rows


def _f(x):
    try:
        return float(x)
    except Exception:
        return None

def _i(x):
    try:
        return int(float(x))
    except Exception:
        return None


# ─────────────────────────── main ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="CSV exportado de la VM (alerts)")
    ap.add_argument("--db", help="copia metrics_vm.db")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.csv:
        alerts = load_alerts_csv(args.csv, args.limit)
    elif args.db:
        alerts = load_alerts_db(args.db, args.limit)
    else:
        print("[ERROR] pasa --csv data/alerts_vm.csv o --db data/metrics_vm.db"); sys.exit(1)

    print(f"Alertas: {len(alerts)}  ({alerts[0]['t0'].date()} .. {alerts[-1]['t0'].date()})")

    # símbolos a bajar (1 fetch por símbolo sobre todo el rango)
    g_start = min(a["t0"] for a in alerts) - timedelta(minutes=WINDOW_PRE_MIN + 60)
    g_end = max(a["t0"] for a in alerts) + timedelta(minutes=WINDOW_POST_MIN + 60)
    # Alpaca free SIP rechaza (403) si el rango toca los últimos ~15 min -> capar a now-20min.
    sip_cap = datetime.now(timezone.utc) - timedelta(minutes=20)
    if g_end > sip_cap:
        g_end = sip_cap
        print(f"[nota] g_end capado a {g_end:%Y-%m-%d %H:%M}Z (límite SIP free); las alertas de "
              f"las últimas ~48h tendrán checkpoints tardíos incompletos.")
    tickers = sorted({a["ticker"] for a in alerts})
    etfs = sorted({SECTOR_ETF.get(t, DEFAULT_ETF) for t in tickers})
    symbols = sorted(set(tickers) | set(etfs) | {"QQQ"})
    print(f"Símbolos a bajar de Alpaca: {len(symbols)} (rango {g_start.date()}..{g_end.date()})")

    t_dl = time.time()
    for i, s in enumerate(symbols, 1):
        try:
            load_symbol(s, g_start, g_end)
            n = len(_bars_cache.get(s, ([],))[0])
            if args.verbose:
                print(f"  [{i}/{len(symbols)}] {s}: {n} barras 1m")
        except Exception as e:
            print(f"  [{i}/{len(symbols)}] {s}: ERROR {str(e)[:80]}")
    print(f"Descarga lista en {time.time()-t_dl:.0f}s")

    results = []
    for a in alerts:
        etf = SECTOR_ETF.get(a["ticker"], DEFAULT_ETF)
        try:
            rec = compute_reaction(a, etf)
        except Exception as e:
            rec = {"status": "error", "error": str(e)[:120]}
        rec.update({"alert_id": a["id"], "ticker": a["ticker"], "t0_utc": a["t0"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "score_ia": a["score"], "direccion": a["direccion"], "pct_estimado": a["pct_estimado"],
                    "tipo_catalizador": a["tipo_catalizador"], "sector_etf": etf,
                    "binance_perp": binance_has_perp(a["ticker"]), "source": a["source"]})
        results.append(rec)
        if args.verbose:
            print(f"  {a['ticker']:6} {rec['t0_utc']} st={rec.get('status'):8} "
                  f"abn4h={rec.get('abn_240m')} mfe={rec.get('mfe_abn')} mae={rec.get('mae_abn')} "
                  f"dir_ok={rec.get('direccion_correcta')}")

    _write_csv(results)
    _print_calibration(results)


def _write_csv(results):
    if not results:
        return
    cols = sorted({k for r in results for k in r.keys()})
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csvmod.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(results)
    print(f"\nDetalle -> {OUT_CSV} ({len(results)} filas)")


def _print_calibration(results):
    ok = [r for r in results if r.get("status") == "closed" and r.get("abn_240m") is not None]
    gaps = sum(1 for r in results if r.get("status") == "data_gap")
    print(f"\nEventos con datos: {len(ok)}/{len(results)}  (data_gap: {gaps})")
    if not ok:
        print("[CALIBRACIÓN] sin eventos cerrados. Revisa el CSV."); return

    print("\n=== score IA -> reacción anormal (mediana) ===")
    print(f"{'score':>5} {'n':>4} {'abn1h':>7} {'abn4h':>7} {'abn24h':>7} {'mfe':>7} {'mae':>7} {'dir_ok':>7}")
    by = {}
    for r in ok:
        by.setdefault(r.get("score_ia"), []).append(r)
    for s in sorted(by, key=lambda x: (x is None, x)):
        g = by[s]
        print(f"{str(s):>5} {len(g):>4} {_med(g,'abn_60m'):>7} {_med(g,'abn_240m'):>7} "
              f"{_med(g,'abn_1440m'):>7} {_med(g,'mfe_abn'):>7} {_med(g,'mae_abn'):>7} {_hit(g):>7}")

    print("\n=== tipo_catalizador -> reacción anormal (mediana) ===")
    print(f"{'tipo':<12} {'n':>4} {'abn1h':>7} {'abn4h':>7} {'abn24h':>7} {'mfe':>7} {'dir_ok':>7}")
    by = {}
    for r in ok:
        by.setdefault(r.get("tipo_catalizador") or "?", []).append(r)
    for t in sorted(by, key=lambda x: -len(by[x])):
        g = by[t]
        print(f"{t[:12]:<12} {len(g):>4} {_med(g,'abn_60m'):>7} {_med(g,'abn_240m'):>7} "
              f"{_med(g,'abn_1440m'):>7} {_med(g,'mfe_abn'):>7} {_hit(g):>7}")

    print("\n=== run-up PRE-t0 (cuán tarde llega la alerta: anormal -2h->t0) por tipo ===")
    print(f"{'tipo':<12} {'n':>4} {'runup2h':>8}")
    for t in sorted(by, key=lambda x: -len(by[x])):
        g = [r for r in by[t] if r.get("runup_pre2h_abn") is not None]
        if g:
            print(f"{t[:12]:<12} {len(g):>4} {_med(g,'runup_pre2h_abn'):>8}")


def _med(g, key):
    vals = sorted(v[key] for v in g if v.get(key) is not None)
    return f"{vals[len(vals)//2]:+.2f}" if vals else "—"


def _hit(g):
    h = [v["direccion_correcta"] for v in g if v.get("direccion_correcta") is not None]
    return f"{100*sum(h)//len(h)}%" if h else "—"


if __name__ == "__main__":
    main()
