"""
sources/sec.py — ingesta de comunicados de la SEC (Securities and Exchange Commission) como
MARKET MOVER oficial. Press releases (acciones de aplicación, reglas) + statements/speeches.

Fuente: RSS público de sec.gov (py3.9, sin Cloudflare). OJO: la SEC EXIGE un User-Agent con
contacto real (si no, 403). Salida: articles formato pipeline, source='SEC'.

Las acciones de la SEC suelen ser sobre una EMPRESA concreta (enforcement) o reglas sectoriales
→ el tracker/IA marca alcance=ticker/sector; algunas (reglas amplias) son macro.
"""
from __future__ import annotations

import os
import logging
import hashlib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

FEEDS = {
    "press":    "https://www.sec.gov/news/pressreleases.rss",
    "speeches": "https://www.sec.gov/news/speeches-statements.rss",
}
# SEC requiere UA con contacto (configurable). Sin esto puede devolver 403.
_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT", "OportunityAlert oscar@pairus.ai")}


def _article(pid: str, text: str, url: str, pub: datetime, feed: str) -> dict:
    age = max(0.0, (datetime.now(timezone.utc) - pub).total_seconds() / 60)
    return {
        "id": f"sec_{pid}",
        "source": "SEC",
        "title": f"[SEC] {text[:170]}",
        "summary": text,
        "url": url,
        "published_at": pub.isoformat(),
        "age_minutes": round(age, 1),
        "tickers_found": [],
        "raw_text": text.upper(),
        "sec_feed": feed,
    }


def fetch_recent(max_age_min: float = 1440, feeds: list[str] | None = None) -> list[dict]:
    out = []
    for name in (feeds or list(FEEDS.keys())):
        url = FEEDS.get(name)
        if not url:
            continue
        try:
            r = requests.get(url, headers=_UA, timeout=20)
            if r.status_code != 200:
                logger.warning(f"[SEC] {name} HTTP {r.status_code}")
                continue
            items = ET.fromstring(r.content).findall(".//item")
        except Exception as e:
            logger.warning(f"[SEC] {name} no disponible: {str(e)[:100]}")
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
    logger.info(f"[SEC] {len(out)} comunicados recientes (<{max_age_min:g}min)")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for a in fetch_recent(max_age_min=100000)[:8]:
        print(f"  age={a['age_minutes']:>8.0f}m [{a['sec_feed']:<8}] {a['summary'][:80]}")
