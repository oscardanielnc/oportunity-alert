"""
sources/treasury.py — ingesta de comunicados del Tesoro de EE.UU. como MARKET MOVER oficial.

⚠️ El Tesoro NO expone RSS/JSON público (probado 2026-06-20) → se hace SCRAPING HTML best-effort
de home.treasury.gov/news/press-releases. Más frágil que un RSS (puede romper si cambian el HTML);
falla con gracia (devuelve []). Contenido market-mover: sanciones (OFAC), deuda, aranceles, TIC.

Sin fecha fiable por-item en el HTML → se trata cada link nuevo como reciente; el dedup del tracker
(por id/fingerprint) evita reprocesar. El poller corre cada ~30min, así que lo vemos poco después.
Salida: articles formato pipeline, source='TREASURY'.
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

URL = "https://home.treasury.gov/news/press-releases"
_UA = {"User-Agent": "OportunityAlert oscar@pairus.ai"}
# links a comunicados individuales: /news/press-releases/sbNNNN  (excluye nav: readouts, testimonies…)
_LINK_RE = re.compile(r'href="(/news/press-releases/(sb\d+))"[^>]*>([^<]{8,120})')


def fetch_recent(max_age_min: float = 1440, max_items: int = 15) -> list[dict]:
    """Scrape best-effort de los press releases del Tesoro. max_age_min se ignora (sin fecha
    por-item); el dedup del tracker controla la repetición. Falla con gracia."""
    out = []
    try:
        r = requests.get(URL, headers=_UA, timeout=20)
        if r.status_code != 200:
            logger.warning(f"[Treasury] HTTP {r.status_code}")
            return out
        html = r.text
    except Exception as e:
        logger.warning(f"[Treasury] no disponible: {str(e)[:100]}")
        return out
    seen = set()
    now = datetime.now(timezone.utc)
    for href, sid, title in _LINK_RE.findall(html):
        if sid in seen:
            continue
        seen.add(sid)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        out.append({
            "id": f"treasury_{sid}",
            "source": "TREASURY",
            "title": f"[Treasury] {title[:170]}",
            "summary": title,
            "url": f"https://home.treasury.gov{href}",
            "published_at": now.isoformat(),
            "age_minutes": 0.0,            # sin fecha fiable; lo vemos al publicarse (poller 30min)
            "tickers_found": [],
            "raw_text": title.upper(),
        })
        if len(out) >= max_items:
            break
    logger.info(f"[Treasury] {len(out)} press releases (scrape best-effort)")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for a in fetch_recent()[:8]:
        print(f"  {a['summary'][:85]}")
