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
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Backend por defecto: mirror RSS (py3.9, sin Cloudflare, sin credenciales). near-real-time.
# El backend directo (truthbrush) queda como upgrade futuro para frescura age-0 (necesita
# py3.10 + proxy residencial por Cloudflare).
RSS_URL = os.environ.get("TRUTH_SOCIAL_RSS", "https://trumpstruth.org/feed")
_UA = {"User-Agent": "Mozilla/5.0 (OportunityAlert)"}

# Cuentas a seguir (configurable). Por defecto Trump; se amplía con las cuentas market-relevant
# que la propia cuenta sigue (ver discover_following). Sin @, como las usa la API.
DEFAULT_HANDLES = ["realDonaldTrump"]

# Gate de relevancia de mercado (reusa el espíritu del trump_tracker): el post debe tocar un
# tema con impacto o nombrar un ticker. Lista amplia; la IA del tracker filtra el ruido fino.
_MACRO_KEYWORDS = (
    "tariff", "tariffs", "trade", "china", "taiwan", "chip", "chips", "semiconductor",
    "semiconductors", "fed", "rate", "rates", "interest", "inflation", "oil", "energy",
    "drug", "drugs", "pharma", "defense", "intel", "nvidia", "apple", "tesla", "deal",
    "sanction", "sanctions", "import", "imports", "export", "exports", "stock", "stocks",
    "market", "markets", "economy", "economic", "factory", "factories", "manufacturing",
    "steel", "crypto", "bitcoin", "hormuz", "opec", "boeing", "treasury",
)
# Regex con límites de palabra: evita que "ai"/"oil" matcheen dentro de "Strait"/"toil", etc.
_RELEVANT_RE = re.compile(r"\b(" + "|".join(_MACRO_KEYWORDS) + r")\b", re.IGNORECASE)

_HTML = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _HTML.sub(" ", s or "").replace("&amp;", "&").replace("&#39;", "'").strip()


def _is_market_relevant(text: str) -> bool:
    return bool(_RELEVANT_RE.search(text))


def _article(pid: str, text: str, url: str, pub: datetime) -> dict:
    """Arma el article en el formato del pipeline (igual que finnhub_news)."""
    age = max(0.0, (datetime.now(timezone.utc) - pub).total_seconds() / 60)
    return {
        "id": f"truth_{pid}",
        "source": "TRUTH_SOCIAL",
        "title": f"[Trump] {text[:160]}",
        "summary": text,
        "url": url,
        "published_at": pub.isoformat(),
        "age_minutes": round(age, 1),
        "tickers_found": [],            # se extraen en el filtro / trump capture
        "raw_text": text.upper(),
    }


def fetch_recent_rss(max_items: int = 30, max_age_min: float = 180) -> list[dict]:
    """Backend RSS (default): lee el mirror trumpstruth.org. Devuelve posts recientes y
    market-relevant en formato article. py3.9, sin Cloudflare, sin credenciales."""
    out = []
    try:
        r = requests.get(RSS_URL, headers=_UA, timeout=20)
        if r.status_code != 200:
            logger.warning(f"[TruthSocial] RSS {r.status_code}")
            return out
        root = ET.fromstring(r.text)
    except Exception as e:
        logger.warning(f"[TruthSocial] RSS no disponible: {str(e)[:100]}")
        return out
    for item in root.findall(".//item")[:max_items]:
        def _g(tag):
            el = item.find(tag)
            return el.text if el is not None else ""
        text = _strip_html(_g("description") or _g("title"))
        if not text:
            continue
        try:
            pub = parsedate_to_datetime(_g("pubDate"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            pub = datetime.now(timezone.utc)
        age = (datetime.now(timezone.utc) - pub).total_seconds() / 60
        if age > max_age_min:
            continue
        if not _is_market_relevant(text):
            continue
        pid = (_g("originalId") or _g("guid")
               or hashlib.md5(text.encode()).hexdigest())
        url = _g("originalUrl") or _g("link") or ""
        out.append(_article(str(pid), text, url, pub))
    logger.info(f"[TruthSocial] {len(out)} posts relevantes (RSS, <{max_age_min:g}min)")
    return out


def _api():
    user = os.environ.get("TRUTH_SOCIAL_USER", "")
    pwd = os.environ.get("TRUTH_SOCIAL_PASS", "")
    if not user or not pwd:
        raise EnvironmentError("TRUTH_SOCIAL_USER / TRUTH_SOCIAL_PASS no están en el entorno")
    from truthbrush import Api   # import perezoso: no romper el arranque si falta la dep
    return Api(user, pwd)


def fetch_recent(max_items: int = 30, max_age_min: float = 180) -> list[dict]:
    """Punto de entrada del conector. Backend por defecto = RSS mirror (robusto en py3.9,
    sin Cloudflare). Para el directo (truthbrush, age-0) usar fetch_recent_api()."""
    return fetch_recent_rss(max_items=max_items, max_age_min=max_age_min)


def fetch_recent_api(handles: list[str] | None = None, max_per_handle: int = 10) -> list[dict]:
    """Backend DIRECTO (truthbrush) — frescura age-0 pero requiere py3.10 + proxy (Cloudflare).
    Maneja el 403 con gracia (loguea y devuelve lo que haya, sin romper el loop)."""
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
                text = _strip_html(post.get("content") or "")
                if not text or not _is_market_relevant(text.upper()):
                    continue
                try:
                    pub = datetime.fromisoformat((post.get("created_at") or "").replace("Z", "+00:00"))
                except Exception:
                    pub = datetime.now(timezone.utc)
                pid = str(post.get("id") or hashlib.md5(text.encode()).hexdigest())
                out.append(_article(pid, text, post.get("url", ""), pub))
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
