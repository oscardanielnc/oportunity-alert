"""
sources/fed.py — ingesta de comunicados de la Reserva Federal (Fed) como MARKET MOVER oficial.

Fuente: RSS público de federalreserve.gov (py3.9, sin Cloudflare, sin credenciales). El feed
MONETARY (FOMC) es el de mayor impacto macro — una decisión de tasas mueve TODO el mercado.

Salida: articles en el formato del pipeline (igual que truth_social/finnhub), source='FED', para
alimentar el tracker de market movers. Los comunicados Fed son MACRO → el tracker/IA los marca
alcance=macro y el scoreboard los mide CRUDO (no anormal vs QQQ).

Nota técnica: los feeds Fed traen BOM → parsear desde r.content (bytes), no r.text.
"""
from __future__ import annotations

import logging
import hashlib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

FEEDS = {
    "monetary":  "https://www.federalreserve.gov/feeds/press_monetary.xml",   # FOMC — el oro
    "press_all": "https://www.federalreserve.gov/feeds/press_all.xml",
    "speeches":  "https://www.federalreserve.gov/feeds/speeches.xml",
}
_UA = {"User-Agent": "Mozilla/5.0 (OportunityAlert)"}


def _article(pid: str, text: str, url: str, pub: datetime, feed: str) -> dict:
    age = max(0.0, (datetime.now(timezone.utc) - pub).total_seconds() / 60)
    return {
        "id": f"fed_{pid}",
        "source": "FED",
        "title": f"[Fed] {text[:170]}",
        "summary": text,
        "url": url,
        "published_at": pub.isoformat(),
        "age_minutes": round(age, 1),
        "tickers_found": [],          # macro: el tracker/IA mapea el impacto
        "raw_text": text.upper(),
        "fed_feed": feed,
    }


def fetch_recent(max_age_min: float = 1440, feeds: list[str] | None = None) -> list[dict]:
    """Trae comunicados recientes de la Fed (default: última 24h). Maneja BOM y errores con gracia."""
    out = []
    for name in (feeds or list(FEEDS.keys())):
        url = FEEDS.get(name)
        if not url:
            continue
        try:
            r = requests.get(url, headers=_UA, timeout=20)
            if r.status_code != 200:
                logger.warning(f"[Fed] {name} HTTP {r.status_code}")
                continue
            items = ET.fromstring(r.content).findall(".//item")   # r.content: maneja BOM
        except Exception as e:
            logger.warning(f"[Fed] {name} no disponible: {str(e)[:100]}")
            continue
        for it in items:
            title = (it.findtext("title") or "").strip()
            desc = (it.findtext("description") or "").strip()
            text = title if len(title) >= len(desc) else desc
            if not text:
                continue
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate"))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except Exception:
                pub = datetime.now(timezone.utc)
            if (datetime.now(timezone.utc) - pub).total_seconds() / 60 > max_age_min:
                continue
            link = it.findtext("link") or ""
            pid = it.findtext("guid") or link or hashlib.md5(text.encode()).hexdigest()
            out.append(_article(str(pid).strip(), text, link, pub, name))
    logger.info(f"[Fed] {len(out)} comunicados recientes (<{max_age_min:g}min)")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for a in fetch_recent(max_age_min=100000)[:8]:
        print(f"  age={a['age_minutes']:>8.0f}m [{a['fed_feed']:<9}] {a['summary'][:80]}")
