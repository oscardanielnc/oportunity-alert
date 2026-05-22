"""
Polling de SEC EDGAR RSS feed — 8-K filings en tiempo real.
Sin API key requerida. Rate limit: 10 req/seg — usamos 1 req/90s.
"""
import logging
import hashlib
import feedparser
import requests
from datetime import datetime, timezone
from dateutil import parser as dateparser
from filters.keyword_filter import ticker_in_text

logger = logging.getLogger(__name__)

EDGAR_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&dateb=&owner=include&count=40"
    "&search_text=&output=atom"
)
SEC_USER_AGENT = "OportunityAlert oscar@pairus.ai"

# Mapa nombre empresa → ticker (EDGAR usa nombres, no símbolos)
# Fragmentos suficientes para matchear en el título del feed
COMPANY_TO_TICKER = {
    "NVIDIA": "NVDA",
    "ADVANCED MICRO DEVICES": "AMD",
    "BROADCOM": "AVGO",
    "MICRON TECHNOLOGY": "MU",
    "INTEL": "INTC",
    "MARVELL TECHNOLOGY": "MRVL",
    "ARM HOLDINGS": "ARM",
    "COHERENT": "COHR",
    "ARISTA NETWORKS": "ANET",
    "CISCO": "CSCO",
    "VERTIV": "VRT",
    "WOLFSPEED": "WOLF",
    "LUMENTUM": "LITE",
    "SANDISK": "SNDK",
    "D-WAVE QUANTUM": "QBTS",
    "IONQ": "IONQ",
    "RIGETTI": "RGTI",
    "QUANTUM COMPUTING INC": "QUBT",
    "PALANTIR": "PLTR",
    "APPLOVIN": "APP",
    "SHOPIFY": "SHOP",
    "MERCADOLIBRE": "MELI",
    "SEA LIMITED": "SE",
    "TAKE-TWO": "TTWO",
    "SOUNDHOUND": "SOUN",
    "REDDIT": "RDDT",
    "CROWDSTRIKE": "CRWD",
    "KRATOS DEFENSE": "KTOS",
    "AEROVIRONMENT": "AVAV",
    "ROCKET LAB": "RKLB",
    "INTUITIVE MACHINES": "LUNR",
    "JOBY AVIATION": "JOBY",
    "ARCHER AVIATION": "ACHR",
    "BIGBEAR": "BBAI",
    "TALEN ENERGY": "TLN",
    "BLOOM ENERGY": "BE",
    "IRIS ENERGY": "IREN",
    "TESLA": "TSLA",
    "RIVIAN": "RIVN",
    "LUCID": "LCID",
    "SUPER MICRO": "SMCI",
    "APPLIED DIGITAL": "APLD",
    "INNODATA": "INOD",
    "ROBINHOOD": "HOOD",
    "SOFI": "SOFI",
    "JUMIA": "JMIA",
    "FLY LEASING": "FLY",
    "GAMESTOP": "GME",
    "AMC ENTERTAINMENT": "AMC",
    "MARATHON DIGITAL": "MARA",
    "RIOT PLATFORMS": "RIOT",
    "MICROSTRATEGY": "MSTR",
}


def _extract_tickers_from_company_name(text_upper: str, watchlist: list[str]) -> list[str]:
    """
    Busca tickers en filings EDGAR. Dos métodos combinados:
    1. Diccionario empresa→ticker (fuente principal, siempre preciso)
    2. Ticker como palabra completa en el texto usando word-boundary regex
       (cubre casos donde EDGAR sí incluye el símbolo, sin falsos positivos)
    """
    found = set()
    # Método 1: diccionario de nombres de empresa
    for company_fragment, ticker in COMPANY_TO_TICKER.items():
        if ticker in watchlist and company_fragment.upper() in text_upper:
            found.add(ticker)
    # Método 2: símbolo como palabra completa (\bQBTS\b encuentra QBTS pero no en otras palabras)
    for t in watchlist:
        if ticker_in_text(t, text_upper):
            found.add(t)
    return list(found)


def fetch_8k_filings(watchlist: list[str]) -> list[dict]:
    """
    Descarga el feed RSS de EDGAR y retorna filings 8-K.
    Filtra por tickers de la watchlist en el nombre de la empresa cuando es posible.

    Retorna lista de dicts con campos normalizados.
    """
    articles = []
    try:
        # feedparser maneja la descarga internamente — agregar User-Agent
        feedparser.USER_AGENT = SEC_USER_AGENT
        feed = feedparser.parse(EDGAR_FEED_URL)

        if feed.bozo and not feed.entries:
            logger.error(f"EDGAR feed error: {feed.bozo_exception}")
            return []

        now_utc = datetime.now(timezone.utc)

        for entry in feed.entries:
            try:
                # Obtener título y resumen del filing
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")
                entry_id = entry.get("id", link)

                # Fecha de publicación
                published_str = entry.get("published", "")
                if published_str:
                    pub_dt = dateparser.parse(published_str)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    age_minutes = (now_utc - pub_dt).total_seconds() / 60
                else:
                    pub_dt = now_utc
                    age_minutes = 0

                # Extraer tickers: primero símbolo directo, luego por nombre de empresa
                full_text = f"{title} {summary}".upper()
                tickers_found = _extract_tickers_from_company_name(full_text, watchlist)

                article = {
                    "id": hashlib.md5(entry_id.encode()).hexdigest(),
                    "source": "SEC_EDGAR",
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "published_at": pub_dt.isoformat(),
                    "age_minutes": round(age_minutes, 1),
                    "tickers_found": tickers_found,
                    "raw_text": full_text,
                }
                articles.append(article)

            except Exception as e:
                logger.warning(f"Error procesando entry EDGAR: {e}")
                continue

        logger.info(f"EDGAR: {len(articles)} filings obtenidos del feed")

    except Exception as e:
        logger.error(f"Error fetching EDGAR feed: {e}")

    return articles


def fetch_company_filing_detail(filing_url: str) -> str:
    """
    Descarga el detalle de un filing específico para extraer más contexto.
    Retorna el texto del índice del filing.
    """
    try:
        resp = requests.get(
            filing_url,
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.text[:5000]  # primeros 5000 chars son suficientes
    except Exception as e:
        logger.warning(f"No se pudo obtener detalle del filing {filing_url}: {e}")
        return ""
