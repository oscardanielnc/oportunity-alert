"""
utils/trump_tracker.py — Radar de declaraciones de Trump que mueven el mercado.

Reusa los feeds que YA llegan al pipeline (Finnhub / Alpaca-Benzinga): detecta los
titulares donde Trump dice algo con potencial de mover una acción concreta o un sector
macro (tariffs, China, guerra, Fed, semiconductores…), los RESUME con IA en lenguaje
simple y, sobre todo, traduce cada uno en QUÉ SIGNIFICA y QUÉ ACCIÓN CONCRETA podría
tomar Oscar según su watchlist y sus posiciones abiertas. Decision support — no ejecuta
ni ordena nada; Oscar decide.

Diseño (Oscar, 2026-06-18):
  - Fuente: feeds existentes (cero APIs nuevas). Alcance: watchlist + posiciones + macro.
  - Captura barata y síncrona (gate por keywords + dedup). La IA corre en un WORKER
    aparte → nunca bloquea el hilo de noticias.
  - Modelo BARATO (como el Brief): TRUMP_ENGINE > DAILY_BRIEF_ENGINE > AI_ENGINE > gemini.

capture_headline(article, watchlist, held) -> bool   # gate + encola (no llama IA)
start_worker()                                        # arranca el worker de resúmenes
get_feed(limit)                                       # items para el endpoint /api/trump
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))
_DATA_DIR  = Path(__file__).parent.parent / "data"
_FEED_PATH = _DATA_DIR / "trump_feed.json"

_MAX_ITEMS = 40           # tope de items guardados (lo más reciente primero)
_DEDUP_WINDOW = 200       # fingerprints recientes que se revisan para no reprocesar

# ── Gate por keywords (código puro, < 1ms) ──────────────────────────────────────
# Trump-céntrico: el titular debe nombrar a Trump (o su plataforma). "potus"/"white
# house" se incluyen porque los feeds suelen titular así sus declaraciones.
_TRUMP_TOKENS = ("trump", "potus", "truth social", "president donald")

# Relevancia macro: si el titular no nombra un ticker del universo, al menos debe tocar
# un tema con impacto de mercado. Lista deliberadamente amplia (la IA filtra el ruido).
_MACRO_KEYWORDS = (
    "tariff", "tariffs", "trade war", "trade deal", "import", "export", "sanction",
    "china", "chinese", "taiwan", "mexico", "canada", "europe", "eu ",
    "fed", "federal reserve", "powell", "interest rate", "rate cut", "rate hike",
    "inflation", "tax", "taxes", "tax cut", "stimulus", "deficit", "debt ceiling",
    "war", "military", "defense", "nato", "ukraine", "russia", "iran", "israel",
    "middle east", "ceasefire", "sanctions", "oil", "energy", "drill",
    "chip", "chips", "semiconductor", "ai ", "artificial intelligence",
    "crypto", "bitcoin", "regulation", "antitrust", "tariff threat", "executive order",
)

# Limpia tokens de mercado para fingerprint/dedup
_WS = re.compile(r"\s+")


def _now_iso() -> str:
    return datetime.now(LIMA).strftime("%Y-%m-%dT%H:%M:%S")


def _text_of(article: dict) -> str:
    return (article.get("raw_text")
            or (article.get("title", "") + " " + article.get("summary", ""))).lower()


def _fingerprint(article: dict) -> str:
    """Hash del titular normalizado: dedup el MISMO statement llegado por varias fuentes."""
    base = (article.get("title") or article.get("raw_text") or "").lower()
    base = _WS.sub(" ", re.sub(r"[^a-z0-9 ]", "", base)).strip()[:160]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _mentions_ticker(text_up: str, universe: set) -> list:
    """Tickers del universo nombrados en el titular (match por palabra completa)."""
    found = []
    for tk in universe:
        if re.search(rf"\b{re.escape(tk)}\b", text_up):
            found.append(tk)
    return found


# ── Cola + worker ───────────────────────────────────────────────────────────────

_q: "queue.Queue" = queue.Queue(maxsize=200)
_worker_started = False
_worker_lock = threading.Lock()
_io_lock = threading.Lock()


def mentions_trump(article: dict) -> bool:
    """Chequeo ultra-barato (substring) para gatear el trabajo más caro en el caller:
    así no leemos el account_cache (held) por cada artículo, solo cuando aparece Trump."""
    try:
        return any(tok in _text_of(article) for tok in _TRUMP_TOKENS)
    except Exception:
        return False


def capture_headline(article: dict, watchlist=None, held=None) -> bool:
    """
    Gate barato + dedup + encola para resumen IA. NO llama a la IA (eso lo hace el worker).
    Retorna True si el titular fue encolado (pasó el gate y no es duplicado).
    """
    try:
        text = _text_of(article)
        if not any(tok in text for tok in _TRUMP_TOKENS):
            return False

        universe = {t.upper() for t in (watchlist or [])} | {t.upper() for t in (held or [])}
        text_up = text.upper()
        tickers = _mentions_ticker(text_up, universe)
        macro_hit = [k.strip() for k in _MACRO_KEYWORDS if k in text]
        if not tickers and not macro_hit:
            return False   # menciona a Trump pero sin relevancia de mercado → ignorar

        fp = _fingerprint(article)
        if _is_duplicate(fp):
            return False

        item = {
            "fp": fp,
            "ts": _now_iso(),
            "headline": (article.get("title") or article.get("raw_text") or "")[:300],
            "url": article.get("url") or article.get("article_url") or "",
            "source": article.get("source", ""),
            "tickers_seed": tickers,
            "macro_seed": macro_hit[:6],
            "held": sorted({t.upper() for t in (held or [])}),
        }
        _q.put_nowait(item)
        logger.info(f"[Trump] capturado: {item['headline'][:80]} "
                    f"(tickers={tickers or '-'}, macro={macro_hit[:3] or '-'})")
        return True
    except queue.Full:
        logger.warning("[Trump] cola llena — titular descartado")
        return False
    except Exception as e:
        logger.debug(f"[Trump] capture error: {e}")
        return False


def start_worker() -> None:
    """Arranca el worker daemon de resúmenes (idempotente)."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, name="TrumpWorker", daemon=True).start()
        _worker_started = True
        logger.info("[Trump] worker de resúmenes iniciado")


def _worker() -> None:
    while True:
        item = _q.get()
        try:
            _process(item)
        except Exception as e:
            logger.warning(f"[Trump] error procesando item: {e}")
        finally:
            _q.task_done()


# ── Capa IA (barata) ─────────────────────────────────────────────────────────────

_SYSTEM = (
    "Eres un analista que ayuda a Oscar, un inversor minorista, a entender si una "
    "declaración del presidente Trump puede mover el mercado y QUÉ hacer al respecto. "
    "Hablas claro y directo, sin jerga. NO das órdenes de comprar/vender como un asesor "
    "regulado: explicas el posible impacto y la acción que Oscar PODRÍA evaluar. "
    "Respondes SIEMPRE en español y tu salida es JSON."
)


def _build_prompt(item: dict) -> str:
    held = ", ".join(item.get("held") or []) or "(ninguna)"
    seed_tk = ", ".join(item.get("tickers_seed") or []) or "(ninguno detectado)"
    seed_macro = ", ".join(item.get("macro_seed") or []) or "(temas macro)"
    return (
        f"Titular: \"{item['headline']}\"\n"
        f"Fuente: {item.get('source', '?')}\n"
        f"Tickers del universo de Oscar mencionados: {seed_tk}\n"
        f"Temas macro detectados: {seed_macro}\n"
        f"Posiciones ABIERTAS de Oscar ahora mismo: {held}\n\n"
        "Evalúa si esta declaración de Trump es REALMENTE capaz de mover el mercado o un "
        "sector/acción relevante para Oscar. Si es ruido (mención tangencial, sin impacto "
        "de mercado, o no es realmente algo que dijo Trump), marca relevante=false.\n\n"
        "Devuelve JSON con EXACTAMENTE estos campos:\n"
        '{\n'
        '  "relevante": true|false,\n'
        '  "resumen": "1-2 frases: qué dijo Trump, en simple",\n'
        '  "significado": "qué implica para el mercado/sector (1-2 frases)",\n'
        '  "accion": "acción concreta que Oscar podría evaluar, nombrando los tickers de '
        'su universo afectados; si no hay nada que hacer, dilo",\n'
        '  "impacto": "alcista|bajista|mixto|incierto",\n'
        '  "alcance": "ticker|sector|macro",\n'
        '  "tickers_afectados": ["TICKER", ...]\n'
        '}'
    )


def _process(item: dict) -> None:
    eng = (os.environ.get("TRUMP_ENGINE")
           or os.environ.get("DAILY_BRIEF_ENGINE")
           or os.environ.get("AI_ENGINE", "gemini")).lower()
    model = os.environ.get("TRUMP_MODEL")
    if eng == "claude" and not model:
        model = "claude-haiku-4-5-20251001"

    data = {}
    try:
        from utils.ai_client import call_ai, parse_json_response
        text = call_ai(_build_prompt(item), system=_SYSTEM, engine=eng, model=model,
                       max_tokens=500, temperature=0.2)
        data = parse_json_response(text) or {}
    except Exception as e:
        logger.warning(f"[Trump] IA no disponible: {e}")
        data = {}

    # Sin IA: guardamos el titular crudo igual (mejor avisar que perderlo), marcado.
    relevante = data.get("relevante", True if not data else bool(data.get("relevante")))
    if data and not relevante:
        logger.info(f"[Trump] IA descartó por no-relevante: {item['headline'][:70]}")
        _remember_fp(item["fp"])   # recordar para no reprocesar el mismo statement
        return

    tickers = data.get("tickers_afectados") or item.get("tickers_seed") or []
    if not isinstance(tickers, list):
        tickers = [str(tickers)]

    record = {
        "fp": item["fp"],
        "ts": item["ts"],
        "headline": item["headline"],
        "url": item.get("url", ""),
        "source": item.get("source", ""),
        "resumen": (data.get("resumen") or "").strip() or item["headline"],
        "significado": (data.get("significado") or "").strip(),
        "accion": (data.get("accion") or "").strip(),
        "impacto": (data.get("impacto") or "incierto").lower(),
        "alcance": (data.get("alcance") or ("ticker" if tickers else "macro")).lower(),
        "tickers": [str(t).upper() for t in tickers][:8],
        "engine": (f"{eng}:{model}" if model else eng) if data else "sin IA",
        "generated_at": _now_iso(),
    }
    _append(record)
    logger.info(f"[Trump] guardado [{record['impacto']}/{record['alcance']}] "
                f"{record['tickers'] or '-'}: {record['resumen'][:80]}")


# ── Persistencia (data/trump_feed.json) ──────────────────────────────────────────

def _load() -> dict:
    try:
        d = json.loads(_FEED_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": [], "fingerprints": []}


def _save(d: dict) -> None:
    try:
        _DATA_DIR.mkdir(exist_ok=True)
        tmp = _FEED_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_FEED_PATH)
    except Exception as e:
        logger.warning(f"[Trump] no pude guardar feed: {e}")


def _is_duplicate(fp: str) -> bool:
    with _io_lock:
        d = _load()
        if fp in (d.get("fingerprints") or []):
            return True
        return any(it.get("fp") == fp for it in d.get("items", []))


def _remember_fp(fp: str) -> None:
    """Recuerda un fp descartado por la IA para no reprocesar el mismo statement repetido."""
    with _io_lock:
        d = _load()
        fps = d.get("fingerprints") or []
        if fp not in fps:
            fps.append(fp)
        d["fingerprints"] = fps[-_DEDUP_WINDOW:]
        _save(d)


def _append(record: dict) -> None:
    with _io_lock:
        d = _load()
        items = [it for it in d.get("items", []) if it.get("fp") != record["fp"]]
        items.insert(0, record)
        d["items"] = items[:_MAX_ITEMS]
        fps = d.get("fingerprints") or []
        if record["fp"] not in fps:
            fps.append(record["fp"])
        d["fingerprints"] = fps[-_DEDUP_WINDOW:]
        _save(d)


def get_feed(limit: int = 20) -> dict:
    """Items resumidos para el dashboard, lo más reciente primero."""
    d = _load()
    items = d.get("items", [])[:max(1, min(limit, _MAX_ITEMS))]
    return {"items": items, "count": len(items)}
