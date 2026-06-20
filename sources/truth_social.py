"""
sources/truth_social.py — ingesta de posts de Truth Social (Trump y cuentas seguidas).

Truth Social es la fuente PRIMARIA: Trump postea ahí ANTES que cualquier wire (age 0). El estudio
2026-06-20 probó que la frescura es EL lever (fresca 57% direccional vs vieja 46%) → cazar sus
posts en origen es la jugada de mayor valor.

Backend: `truthbrush` (no oficial, autentica con user/pass). Credenciales por env:
  TRUTH_SOCIAL_USER / TRUTH_SOCIAL_PASS  (cuenta dedicada, no personal).

⚠️ LIMITACIÓN CONOCIDA: Truth Social está tras Cloudflare; desde IPs de datacenter (este sandbox
y posiblemente la VM Oracle) la auth devuelve 403 "just a moment". Si la VM está bloqueada, las
opciones son: (A) proxy residencial para truthbrush, (B) mirror RSS público, (C) correr el poller
desde una IP no-datacenter. El conector está hecho con backend PLUGGABLE para enchufar cualquiera.

Salida: lista de articles en el formato del pipeline (igual que finnhub_news), para alimentar el
Trump tracker (capture_headline) y, si nombran un ticker de watchlist, process_article.
"""
from __future__ import annotations

import os
import re
import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cuentas a seguir (configurable). Por defecto Trump; se amplía con las cuentas market-relevant
# que la propia cuenta sigue (ver discover_following). Sin @, como las usa la API.
DEFAULT_HANDLES = ["realDonaldTrump"]

# Gate de relevancia de mercado (reusa el espíritu del trump_tracker): el post debe tocar un
# tema con impacto o nombrar un ticker. Lista amplia; la IA del tracker filtra el ruido fino.
_MACRO_KEYWORDS = (
    "tariff", "tariffs", "trade", "china", "taiwan", "chip", "chips", "semiconductor",
    "fed", "rate", "rates", "inflation", "oil", "energy", "drug", "pharma", "defense",
    "ai", "intel", "nvidia", "apple", "tesla", "deal", "sanction", "import", "export",
    "stock", "market", "economy", "factory", "manufactur", "steel", "crypto", "bitcoin",
)

_HTML = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _HTML.sub(" ", s or "").replace("&amp;", "&").replace("&#39;", "'").strip()


def _is_market_relevant(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _MACRO_KEYWORDS)


def _to_article(post: dict, handle: str) -> dict:
    """Convierte un status de Truth Social al formato de article del pipeline."""
    text = _strip_html(post.get("content") or "")
    created = post.get("created_at") or ""
    try:
        pub = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except Exception:
        pub = datetime.now(timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - pub).total_seconds() / 60)
    pid = str(post.get("id") or hashlib.md5(text.encode()).hexdigest())
    return {
        "id": f"truth_{pid}",
        "source": "TRUTH_SOCIAL",
        "title": f"[{handle}] {text[:160]}",
        "summary": text,
        "url": post.get("url", ""),
        "published_at": pub.isoformat(),
        "age_minutes": round(age, 1),
        "tickers_found": [],            # se extraen en el filtro / trump capture
        "raw_text": text.upper(),
    }


def _api():
    user = os.environ.get("TRUTH_SOCIAL_USER", "")
    pwd = os.environ.get("TRUTH_SOCIAL_PASS", "")
    if not user or not pwd:
        raise EnvironmentError("TRUTH_SOCIAL_USER / TRUTH_SOCIAL_PASS no están en el entorno")
    from truthbrush import Api   # import perezoso: no romper el arranque si falta la dep
    return Api(user, pwd)


def fetch_recent(handles: list[str] | None = None, max_per_handle: int = 10) -> list[dict]:
    """Trae posts recientes de las cuentas dadas, ya filtrados por relevancia de mercado.
    Maneja el 403 de Cloudflare con gracia (loguea y devuelve lo que haya, sin romper el loop)."""
    handles = handles or DEFAULT_HANDLES
    out = []
    try:
        api = _api()
    except Exception as e:
        logger.warning(f"[TruthSocial] no disponible: {e}")
        return out
    for h in handles:
        try:
            import itertools
            for post in itertools.islice(api.pull_statuses(h, replies=False), max_per_handle):
                art = _to_article(post, h)
                if _is_market_relevant(art["raw_text"]):
                    out.append(art)
        except Exception as e:
            # 403 Cloudflare u otros: no abortar el resto de handles
            logger.warning(f"[TruthSocial] pull '{h}' falló (¿Cloudflare 403?): {str(e)[:120]}")
    logger.info(f"[TruthSocial] {len(out)} posts relevantes de {len(handles)} cuentas")
    return out


def discover_following(handle: str | None = None) -> list[str]:
    """Lista las cuentas que sigue la cuenta autenticada (para evaluar cuáles añadir).
    Útil porque la cuenta nueva ya sigue cuentas por defecto (algunas pueden mover mercado)."""
    try:
        api = _api()
        me = handle or os.environ.get("TRUTH_SOCIAL_USER", "")
        return [u.get("username") or u.get("acct") for u in api.user_following(me)]
    except Exception as e:
        logger.warning(f"[TruthSocial] discover_following falló: {str(e)[:120]}")
        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Sigue:", discover_following())
    for a in fetch_recent():
        print(f"  age={a['age_minutes']}m | {a['title'][:90]}")
