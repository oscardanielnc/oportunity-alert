"""
IPO event-study dataset builder (sección nueva: estrategia de IPOs).

Reconstruye, SIN sesgo de supervivencia, la trayectoria de precio de cada IPO desde
su primer día de cotización:
  - Lista de IPOs: Finnhub /calendar/ipo (symbol, fecha, precio de salida, nº acciones,
    valor del deal, status). Incluye TODOS los 'priced' (no solo los que sobrevivieron).
  - Precios día-a-día: yfinance desde la fecha de IPO (~1 año = 252 barras).
  - Benchmark: QQQ (para retorno en EXCESO, separa el patrón IPO del mercado).

Métricas por IPO: precio de salida, 1er close, pop%, deal_size (proxy de hype/tamaño),
y la serie diaria de close + excess vs QQQ alineada por día de trading relativo (t=0).

Caché por ticker (data/ipo_cache/) → reanudable y barato re-correr.
Uso: python -m research.ipo_dataset --from 2021 --to 2024
"""
from __future__ import annotations
import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ROOT = Path(__file__).parent.parent
CACHE_DIR = ROOT / "data" / "ipo_cache"
CALENDAR_JSON = ROOT / "data" / "ipo_calendar.json"
DATASET_JSON = ROOT / "data" / "ipo_dataset.json"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

# Filtros de tradeabilidad (v1)
US_EXCHANGES = ("NYSE", "NASDAQ", "NYSE MKT", "NYSE Arca")
MIN_PRICE = 4.0          # evita penny stocks
MIN_DEAL_USD = 50e6      # deal >= $50M → con liquidez/cobertura institucional real
TRAJ_DAYS = 252          # ~1 año de trading para cubrir el lockup (~180d) + recuperación
BENCHMARK = "QQQ"


def _http_json(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_ipo_calendar(year_from: int, year_to: int) -> list[dict]:
    """Trae el IPO calendar de Finnhub por trimestres (cacheado a disco)."""
    if CALENDAR_JSON.exists():
        cached = json.loads(CALENDAR_JSON.read_text())
        if cached.get("from") == year_from and cached.get("to") == year_to:
            print(f"[calendar] caché: {len(cached['rows'])} IPOs")
            return cached["rows"]
    if not FINNHUB_KEY:
        raise SystemExit("FINNHUB_API_KEY no configurada")

    rows: list[dict] = []
    for y in range(year_from, year_to + 1):
        for q_start in ("01-01", "04-01", "07-01", "10-01"):
            start = f"{y}-{q_start}"
            sd = datetime.strptime(start, "%Y-%m-%d")
            ed = (sd + timedelta(days=95)).strftime("%Y-%m-%d")
            url = f"https://finnhub.io/api/v1/calendar/ipo?from={start}&to={ed}&token={FINNHUB_KEY}"
            try:
                d = _http_json(url)
                chunk = d.get("ipoCalendar", []) or []
                rows.extend(chunk)
                print(f"[calendar] {start}..{ed}: {len(chunk)}")
            except Exception as e:
                print(f"[calendar] ERR {start}: {e}")
            time.sleep(1.1)   # rate-limit friendly (free tier 60/min)

    # Dedup por (symbol, date)
    seen = set()
    uniq = []
    for r in rows:
        k = (r.get("symbol"), r.get("date"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    CALENDAR_JSON.write_text(json.dumps({"from": year_from, "to": year_to, "rows": uniq}))
    print(f"[calendar] total unicos: {len(uniq)}")
    return uniq


def filter_ipos(rows: list[dict]) -> list[dict]:
    """Filtra a IPOs 'priced', US, sobre umbral de precio y tamaño de deal."""
    out = []
    for r in rows:
        if r.get("status") != "priced":
            continue
        sym = r.get("symbol")
        if not sym:
            continue
        exch = (r.get("exchange") or "")
        if not any(exch.startswith(e) for e in US_EXCHANGES):
            continue
        try:
            px = float(r.get("price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        deal = float(r.get("totalSharesValue") or 0)
        if px < MIN_PRICE or deal < MIN_DEAL_USD:
            continue
        # Excluir SPACs: ticker de unidad termina en 'U' y/o sale exactamente a $10
        # (los SPACs cotizan planos a $10 por diseño → contaminan el patrón de IPO operativa)
        if sym.endswith("U") or abs(px - 10.0) < 0.01:
            continue
        out.append({
            "symbol": sym, "name": r.get("name"), "date": r.get("date"),
            "exchange": exch, "ipo_price": px,
            "shares": r.get("numberOfShares"), "deal_usd": deal,
        })
    print(f"[filter] {len(out)} IPOs priced/US/>={MIN_PRICE}$/>=${MIN_DEAL_USD/1e6:.0f}M")
    return out


def _bench_history():
    import yfinance as yf
    h = yf.Ticker(BENCHMARK).history(start="2014-01-01", auto_adjust=False)
    return {d.strftime("%Y-%m-%d"): float(c) for d, c in zip(h.index, h["Close"])}


def fetch_trajectory(sym: str, ipo_date: str) -> dict | None:
    """Trae la trayectoria diaria desde la IPO (cacheada por ticker)."""
    cache = CACHE_DIR / f"{sym}_{ipo_date}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    import yfinance as yf
    try:
        start = ipo_date
        end = (datetime.strptime(ipo_date, "%Y-%m-%d") + timedelta(days=int(TRAJ_DAYS * 1.6))).strftime("%Y-%m-%d")
        h = yf.Ticker(sym).history(start=start, end=end, auto_adjust=False)
        if h.empty or len(h) < 5:
            cache.write_text(json.dumps({"empty": True}))
            return None
        dates = [d.strftime("%Y-%m-%d") for d in h.index][:TRAJ_DAYS]
        closes = [float(c) for c in h["Close"]][:TRAJ_DAYS]
        vols = [float(v) for v in h["Volume"]][:TRAJ_DAYS]
        out = {"symbol": sym, "ipo_date": ipo_date, "dates": dates, "closes": closes, "volumes": vols}
        cache.write_text(json.dumps(out))
        return out
    except Exception as e:
        print(f"[traj] ERR {sym}: {e}")
        return None


def build(year_from: int, year_to: int):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rows = filter_ipos(fetch_ipo_calendar(year_from, year_to))
    print("[bench] cargando QQQ...")
    bench = _bench_history()

    dataset = []
    for i, ipo in enumerate(rows):
        traj = fetch_trajectory(ipo["symbol"], ipo["date"])
        if not traj or traj.get("empty"):
            continue
        closes = traj["closes"]
        first_close = closes[0]
        pop = (first_close - ipo["ipo_price"]) / ipo["ipo_price"] * 100
        # Excess vs QQQ alineado por día (close[t]/close[0] - qqq[t]/qqq[0])
        bdate0 = bench.get(traj["dates"][0])
        excess = []
        if bdate0:
            for d, c in zip(traj["dates"], closes):
                bq = bench.get(d)
                if bq:
                    excess.append((c / first_close - 1) * 100 - (bq / bdate0 - 1) * 100)
                else:
                    excess.append(None)
        rec = {
            **ipo,
            "first_close": first_close,
            "pop_pct": round(pop, 2),
            "n_days": len(closes),
            "closes": closes,
            "excess_vs_qqq": [round(x, 2) if x is not None else None for x in excess],
        }
        dataset.append(rec)
        if (i + 1) % 25 == 0:
            print(f"[build] {i+1}/{len(rows)} procesados...")
        time.sleep(0.15)

    DATASET_JSON.write_text(json.dumps(dataset))
    print(f"[build] LISTO: {len(dataset)} IPOs con trayectoria -> {DATASET_JSON.name}")
    return dataset


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="yf", type=int, default=2021)
    ap.add_argument("--to", dest="yt", type=int, default=2024)
    a = ap.parse_args()
    build(a.yf, a.yt)
