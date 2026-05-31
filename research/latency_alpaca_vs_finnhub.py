#!/usr/bin/env python3
"""
Mide la LATENCIA REAL de Alpaca (feed Benzinga, WebSocket push) vs Finnhub
(REST polling) sobre la misma watchlist, en paralelo, durante una ventana.

Por qué `received_at` y no `created_at`: lo que importa para entrar a un trade es
cuándo NUESTRO sistema tuvo la noticia disponible para actuar — no la hora nominal
de publicación que reporta cada proveedor. Alpaca es push (received_at ≈ publicación);
Finnhub es poll + lag de batch del proveedor (received_at = ciclo de poll que la trajo).

Correr una MAÑANA de mercado (martes-jueves, ~8:30-10:00 Lima) en la VM o local:
    python -m research.latency_alpaca_vs_finnhub --minutes 90
    python -m research.latency_alpaca_vs_finnhub            # corre hasta Ctrl+C

Salida: log crudo en research/latency_log.jsonl + reporte de matching al final
(o al cortar con Ctrl+C). NO interfiere con el servicio de producción: usa sus
propias conexiones, no toca dedup ni envía alertas.
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from sources.alpaca_news import stream_news
from sources.finnhub_news import fetch_company_news

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latency_log.jsonl")

# Matching cross-source: misma noticia si comparten >=1 ticker, llegan dentro de
# MATCH_WINDOW_S una de otra, y los titulares se parecen >= MATCH_RATIO.
MATCH_WINDOW_S = 1800     # 30 min: Finnhub puede lagear bastante vs el push de Alpaca
MATCH_RATIO    = 0.60

_events: list[dict] = []
_events_lock = threading.Lock()
_stop = threading.Event()


def _now() -> float:
    return time.time()


def _load_watchlist() -> list[str]:
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    wl = cfg.get("watchlist", {})
    out = []
    for group in ("primary", "extended", "crypto"):
        out += wl.get(group, [])
    return list(dict.fromkeys(out))


def _record(source: str, news_id: str, headline: str, tickers: list, created_at: str):
    ev = {
        "source": source,
        "id": str(news_id),
        "headline": headline or "",
        "tickers": tickers or [],
        "created_at": created_at or "",
        "received_at": _now(),
        "received_iso": datetime.now(timezone.utc).isoformat(),
    }
    with _events_lock:
        _events.append(ev)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    tk = ",".join(ev["tickers"][:3])
    print(f"  [{source:7}] {tk:18} {headline[:60]}")


# ── Fuente 1: Alpaca WebSocket (push) ────────────────────────────────────────

def _alpaca_thread(watchlist):
    def on_article(a):
        _record("ALPACA", a.get("id", ""), a.get("title", ""),
                a.get("tickers_found", []), a.get("published_at", ""))
    try:
        stream_news(watchlist, on_article, stop_event=_stop)
    except Exception as e:
        print(f"[latency] alpaca thread error: {e}")


# ── Fuente 2: Finnhub REST polling (igual que producción) ────────────────────

def _finnhub_thread(watchlist, interval=120):
    seen = set()
    idx = 0
    batch_size = 5
    while not _stop.is_set():
        batch = watchlist[idx: idx + batch_size]
        idx = (idx + batch_size) % max(len(watchlist), 1)
        for tk in batch:
            if _stop.is_set():
                break
            try:
                for art in fetch_company_news(tk, hours_back=2):
                    nid = art.get("id", "")
                    if nid and nid not in seen:
                        seen.add(nid)
                        _record("FINNHUB", nid, art.get("title", ""),
                                art.get("tickers_found", []), art.get("published_at", ""))
            except Exception as e:
                print(f"[latency] finnhub {tk}: {e}")
        _stop.wait(interval / max(len(watchlist) / batch_size, 1))


# ── Análisis: matching cross-source + reporte ────────────────────────────────

def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", h.lower()).strip()


def _analyze():
    with _events_lock:
        evs = list(_events)
    alpaca = [e for e in evs if e["source"] == "ALPACA"]
    finnhub = [e for e in evs if e["source"] == "FINNHUB"]

    print("\n" + "=" * 70)
    print("REPORTE DE LATENCIA — Alpaca (Benzinga/WS) vs Finnhub (REST)")
    print("=" * 70)
    print(f"Noticias capturadas:  ALPACA={len(alpaca)}  FINNHUB={len(finnhub)}")

    if not alpaca and not finnhub:
        print("Sin noticias en la ventana. Correr en una mañana de mercado activa.")
        return

    # Match: para cada noticia de Alpaca, buscar la misma en Finnhub
    leads = []          # segundos de adelanto de Alpaca (positivo = Alpaca primero)
    alpaca_only = 0
    matched_pairs = []
    used_finnhub = set()
    for a in alpaca:
        a_tk = set(a["tickers"])
        a_norm = _norm(a["headline"])
        best = None
        best_ratio = 0.0
        for i, f in enumerate(finnhub):
            if i in used_finnhub:
                continue
            if not (a_tk & set(f["tickers"])):
                continue
            if abs(a["received_at"] - f["received_at"]) > MATCH_WINDOW_S:
                continue
            ratio = SequenceMatcher(None, a_norm, _norm(f["headline"])).ratio()
            if ratio >= MATCH_RATIO and ratio > best_ratio:
                best, best_ratio, best_i = f, ratio, i
        if best is not None:
            used_finnhub.add(best_i)
            lead = best["received_at"] - a["received_at"]
            leads.append(lead)
            matched_pairs.append((a, best, lead))
        else:
            alpaca_only += 1

    print(f"Noticias en AMBAS fuentes (matched): {len(leads)}")
    print(f"Noticias SOLO en Alpaca:             {alpaca_only}")
    print(f"Noticias SOLO en Finnhub:            {len(finnhub) - len(used_finnhub)}")

    if leads:
        leads_sorted = sorted(leads)
        n = len(leads_sorted)
        median = leads_sorted[n // 2]
        mean = sum(leads) / n
        alpaca_first = sum(1 for l in leads if l > 0)
        print("\nADELANTO de Alpaca vs Finnhub (en las noticias matched):")
        print(f"  Mediana: {median:+.0f} s ({median/60:+.1f} min)")
        print(f"  Media:   {mean:+.0f} s ({mean/60:+.1f} min)")
        print(f"  Alpaca llegó primero en {alpaca_first}/{n} casos")
        print("\n  Detalle (peor→mejor adelanto de Alpaca):")
        for a, f, lead in sorted(matched_pairs, key=lambda x: x[2])[:15]:
            print(f"   {lead:+6.0f}s  {','.join(a['tickers'][:2]):12} {a['headline'][:50]}")

    # Edad al llegar (frescura): created_at vs received_at, solo Alpaca
    fresh = []
    for a in alpaca:
        try:
            c = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")).timestamp()
            fresh.append(a["received_at"] - c)
        except Exception:
            pass
    if fresh:
        fs = sorted(fresh)
        print(f"\nFRESCURA Alpaca (received - published): mediana {fs[len(fs)//2]:.0f}s "
              f"({fs[len(fs)//2]/60:.1f} min) sobre {len(fs)} noticias")

    print("\nVEREDICTO sugerido:")
    if leads:
        median = sorted(leads)[len(leads) // 2]
        if median >= 600:
            print("  Alpaca adelanta >=10 min consistentes → ventaja REAL y accionable.")
        elif median >= 120:
            print("  Alpaca adelanta 2-10 min → ventaja moderada, útil para catalizadores.")
        elif median > 0:
            print("  Adelanto marginal (<2 min) → poca diferencia práctica vs Finnhub.")
        else:
            print("  Finnhub llegó antes en promedio → revisar (¿Alpaca limitado en estos tickers?).")
    else:
        print("  Sin noticias en común para comparar. Repetir en una mañana más activa.")
    print(f"\nLog crudo: {LOG_PATH}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0,
                    help="duración en minutos (0 = hasta Ctrl+C)")
    args = ap.parse_args()

    watchlist = _load_watchlist()
    print(f"Watchlist: {len(watchlist)} tickers. Midiendo latencia Alpaca vs Finnhub...")
    print(f"(Ctrl+C para terminar y ver el reporte){' — limite %.0f min' % args.minutes if args.minutes else ''}\n")

    def _handle_sigint(signum, frame):
        print("\n[latency] terminando...")
        _stop.set()
    signal.signal(signal.SIGINT, _handle_sigint)

    t1 = threading.Thread(target=_alpaca_thread, args=(watchlist,), daemon=True)
    t2 = threading.Thread(target=_finnhub_thread, args=(watchlist,), daemon=True)
    t1.start()
    t2.start()

    deadline = _now() + args.minutes * 60 if args.minutes else None
    try:
        while not _stop.is_set():
            if deadline and _now() >= deadline:
                _stop.set()
                break
            time.sleep(1)
    except KeyboardInterrupt:
        _stop.set()

    time.sleep(1)
    _analyze()
    # Salida forzada: los threads daemon pueden estar bloqueados en ws.recv()
    # (hasta 60s) o en el poll de Finnhub. Ya tenemos el reporte, no esperamos.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
