"""
Capa 2: Filtro rápido Python (<5ms por noticia).
Tres niveles de keyword para minimizar llamadas a Claude:
  TIER_1: 1 sola keyword ya justifica llamar a Claude (eventos de alto impacto)
  TIER_2: necesita 2+ keywords, o 1 keyword + señales de refuerzo
  WEAK:   degradan score, no descartan solos

EDGAR bypass: cualquier 8-K de empresa en watchlist pasa directo a los conviction
gates sin requerir keyword score — las 8-Ks tienen texto legal seco que no matchea
titulares de noticias, pero cualquier filing material es inherentemente relevante.
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tier 1: 1 sola keyword → siempre pasa a Claude ───────────────────────────
# Eventos de máxima especificidad — casi imposible que sea ruido
TIER_1_KEYWORDS = [
    # Gobierno / contratos directos
    "CHIPS ACT", "DOD CONTRACT", "DEPARTMENT OF DEFENSE CONTRACT",
    "GOVERNMENT AWARD", "FEDERAL CONTRACT AWARD", "CONTRACT AWARD",
    "AWARDED CONTRACT", "WINS CONTRACT", "WON CONTRACT",
    "GOVERNMENT STAKE", "EQUITY STAKE",
    "BILLION CONTRACT", "BILLION DEAL", "BILLION AWARD",
    "DOD AWARD", "PENTAGON CONTRACT", "PENTAGON AWARD", "DEFENSE CONTRACT",
    # FDA
    "FDA APPROVAL", "FDA APPROVED", "FDA REJECTED", "FDA CLEARANCE",
    "FDA GRANTS", "RECEIVES FDA", "CLEARED BY FDA",
    "NDA APPROVAL", "BLA APPROVAL", "PDUFA", "BREAKTHROUGH THERAPY",
    # M&A
    "ACQUISITION", "MERGER AGREEMENT", "TAKEOVER BID", "BUYOUT OFFER",
    "TO BE ACQUIRED", "GOING PRIVATE", "TENDER OFFER",
    "AGREES TO ACQUIRE", "AGREED TO ACQUIRE", "TO ACQUIRE",
    # Earnings directos
    "BEATS ESTIMATES", "EPS BEAT", "REVENUE BEAT", "BEATS EXPECTATIONS",
    "EARNINGS BEAT", "BEATS ANALYST", "TOPPED ESTIMATES", "BEATS CONSENSUS",
    "ABOVE CONSENSUS", "TOPPED CONSENSUS",
    "RAISES GUIDANCE", "GUIDANCE RAISED", "RAISED GUIDANCE", "LIFTS GUIDANCE",
    "INCREASES GUIDANCE", "GUIDE HIGHER", "RECORD REVENUE", "RECORD EARNINGS",
    "RECORD PROFIT", "RECORD QUARTERLY",
    # Short squeeze
    "SHORT SQUEEZE", "GAMMA SQUEEZE",
    # EDGAR 8-K items materiales — texto estándar en filings SEC
    "ITEM 1.01", "ITEM 2.01", "ITEM 2.02", "ITEM 7.01", "ITEM 8.01",
    "MATERIAL DEFINITIVE AGREEMENT", "RESULTS OF OPERATIONS",
    "ENTRY INTO A MATERIAL",
    # Upgrades de analista / subida de price target — 1 sola basta (la IA discrimina
    # magnitud). Validado: en large-caps un upgrade mueve el precio (caso MU 26-may,
    # UBS +204% PT) y antes se descartaba por pesar solo 2 pts en Tier-2.
    "PRICE TARGET RAISED", "RAISES PRICE TARGET", "RAISES TARGET", "RAISES PT",
    "PRICE TARGET INCREASED", "BOOSTS PRICE TARGET", "LIFTS PRICE TARGET",
    "UPGRADED TO BUY", "UPGRADED TO OVERWEIGHT", "RAISED TO BUY",
    "UPGRADES TO BUY", "UPGRADES TO OVERWEIGHT",
    # Endorsement de peso / hito de capitalización — 1 sola basta. Caso MRVL 2-jun:
    # Jensen Huang (CEO Nvidia) la llamó "next trillion-dollar company" en Computex y
    # voló ~20-29%; el titular llegó en tiempo real (Benzinga age 0) pero con score 0
    # porque el filtro no reconocía este lenguaje → se filtraba antes de la IA.
    "TRILLION-DOLLAR COMPANY", "TRILLION DOLLAR COMPANY",
    "TRILLION-DOLLAR GIANT", "TRILLION DOLLAR GIANT",
    "NEXT TRILLION", "$1 TRILLION CLUB", "1 TRILLION CLUB",
    "JOIN THE $1 TRILLION", "TRILLION-DOLLAR CLUB",
    "ENDORSES", "ENDORSEMENT FROM", "CALLS IT THE NEXT",
]

# ── Tier 2: necesita 2+ hits para pasar a Claude ──────────────────────────────
# Señales reales pero más comunes — solas pueden ser ruido
TIER_2_KEYWORDS = [
    "GRANT", "MILLION AWARD",
    "DEPARTMENT OF COMMERCE", "DEFENSE DEPARTMENT",
    "CLINICAL TRIAL RESULTS", "PHASE 3", "PHASE 2",
    "STRATEGIC REVIEW", "MERGER", "JOINT VENTURE",
    # (upgrades concretos promovidos a TIER_1 — aquí quedan los más débiles/ruidosos)
    "STRONG BUY", "INITIATED COVERAGE", "OUTPERFORM",
    "INVESTOR DAY", "ANALYST DAY", "SPECIAL DIVIDEND",
    "SHARE BUYBACK", "STOCK BUYBACK",
    "PARTNERSHIP ANNOUNCED", "STRATEGIC PARTNERSHIP",
    "SUPPLY AGREEMENT", "LICENSE AGREEMENT", "MULTI-YEAR AGREEMENT",
    "INSIDER BUYING", "CEO PURCHASED", "CFO PURCHASED",
    "SECURES CONTRACT", "SELECTED BY",
    "MEMORANDUM OF UNDERSTANDING",
    "UNUSUAL OPTIONS",
    # Earnings con lenguaje periodístico real
    "QUARTERLY RESULTS", "QUARTERLY EARNINGS", "FULL YEAR RESULTS",
    "Q1 RESULTS", "Q2 RESULTS", "Q3 RESULTS", "Q4 RESULTS",
    "Q1 EARNINGS", "Q2 EARNINGS", "Q3 EARNINGS", "Q4 EARNINGS",
    "ABOVE EXPECTATIONS", "BETTER THAN EXPECTED", "ABOVE ESTIMATES",
    "TOPPED ANALYST", "EXCEEDED ESTIMATES", "SURPASSED ESTIMATES",
    "GUIDANCE ABOVE", "GUIDANCE RAISED",
    # Contratos (variantes periodísticas)
    "CONTRACT WORTH", "AWARDED DEAL", "SECURES DEAL",
    "MULTI-YEAR CONTRACT", "LONG-TERM CONTRACT",
]

# Todas las keywords críticas juntas (para fingerprint de dedup)
CRITICAL_KEYWORDS = TIER_1_KEYWORDS + TIER_2_KEYWORDS

# ── Keywords que indican ya está priceado (titular) ───────────────────────────
PRICED_HEADLINE_WORDS = [
    "STOCK SOARS", "STOCK SURGES", "STOCK JUMPS", "SHARES SOAR",
    "SHARES SURGE", "SHARES JUMP", "STOCK SKYROCKETS",
    "SPIKES ON", "SOARS ON", "SURGES ON", "RALLIES ON",
]

# ── Keywords débiles (reducen score pero no descartan solos) ──────────────────
WEAK_KEYWORDS = [
    "RUMORED", "SOURCES SAY", "COULD CONSIDER", "MIGHT CONSIDER",
    "ANALYST SPECULATES", "MARKET CHATTER", "UNCONFIRMED",
    "COULD BE", "MIGHT BE", "REPORTEDLY CONSIDERING",
]


def score_article(raw_text: str) -> tuple[int, list[str]]:
    """
    Calcula el score de relevancia de un artículo (0-10).
    Retorna (score, keywords_encontradas).
    Score >= 3 → pasar a Claude.
    Score 1-2 → borderline, no llegar a Claude (ahorra llamadas).
    """
    text_upper = raw_text.upper()
    found = []
    score = 0

    for kw in TIER_1_KEYWORDS:
        if kw in text_upper:
            score += 4  # tier 1 pesa mucho
            found.append(kw)

    for kw in TIER_2_KEYWORDS:
        if kw in text_upper:
            score += 2
            found.append(kw)

    for kw in WEAK_KEYWORDS:
        if kw in text_upper:
            score -= 1  # penalizar señales débiles

    return max(score, 0), found


# Ventana de edad máxima por fuente.
# Finnhub: noticias de earnings post-market tienen lag de 45-90 min en el batch.
# EDGAR:   8-Ks se publican en cualquier momento; siempre son eventos frescos
#          para la empresa aunque el sistema los procese con retraso.
_MAX_AGE_BY_SOURCE = {
    "SEC_EDGAR":      240,   # 4h — 8-K puede procesarse tarde pero sigue siendo material
    "FINNHUB_YAHOO":   90,   # 1.5h — cubre earnings post-market con lag de batch
    "FINNHUB_MARKET":  60,   # 1h  — noticias generales de mercado
}
_DEFAULT_MAX_AGE = 90        # default para cualquier fuente no listada (era 45)


def passes_filter(
    article: dict,
    watchlist: list[str],
    seen_ids: set,
    max_age_minutes: int = None,
) -> tuple[bool, Optional[str]]:
    """
    Retorna (True, None) si pasa el filtro, o (False, razón) si no pasa.

    Score mínimo para pasar:
      - Tier 1 solo (score >= 4): pasa siempre
      - Tier 2 con 2+ keywords (score >= 4): pasa
      - Score < 3: no llega a Claude (ahorra API calls)

    EDGAR bypass: si source=SEC_EDGAR y hay ticker en watchlist, pasa siempre
    sin requerir keywords (8-Ks tienen texto legal que no matchea titulares).
    Los conviction gates filtran el ruido.

    Age filter: source-aware — Finnhub earnings news tiene lag de batch de 45-90 min,
    EDGAR puede procesarse hasta 4h después de publicado y sigue siendo material.
    """
    article_id = article.get("id", "")
    raw_text = article.get("raw_text", "").upper()
    age = article.get("age_minutes", 999)
    source = article.get("source", "")

    # 1. Deduplicación por ID
    if article_id in seen_ids:
        return False, "duplicado"

    # 2. Edad máxima — source-aware
    # max_age_minutes como parámetro solo se usa si se pasa explícitamente (override manual)
    if max_age_minutes is not None:
        effective_max_age = max_age_minutes
    elif source == "SEC_EDGAR":
        effective_max_age = 240   # 8-K siempre material aunque se procese tarde
    elif source == "FINNHUB_MARKET":
        effective_max_age = 60    # noticias generales de mercado
    elif source.startswith("FINNHUB"):
        effective_max_age = 90    # cubre FINNHUB_YAHOO, FINNHUB_REUTERS, etc.
    elif source.startswith("ALPACA"):
        effective_max_age = 120   # push real-time (Benzinga); margen por si el server bufferea
    else:
        effective_max_age = _DEFAULT_MAX_AGE

    if age > effective_max_age:
        return False, f"demasiado vieja ({age:.0f} min, max={effective_max_age})"

    # 3. Ticker en watchlist (word-boundary: evita LITE en Litecoin, SE en Bancshares)
    tickers_in_text = article.get("tickers_found", [])
    if not tickers_in_text:
        tickers_in_text = extract_tickers_from_text(raw_text, watchlist)
    if not tickers_in_text:
        return False, "sin ticker de watchlist"
    article["tickers_found"] = tickers_in_text  # asegurar que está seteado

    # 4. Score de keywords
    score, found_keywords = score_article(raw_text)
    article["keyword_score"] = score
    article["keywords_found"] = found_keywords

    # EDGAR bypass: 8-Ks de empresas en watchlist pasan sin requerir keyword score.
    # Cualquier filing material es inherentemente relevante — las conviction gates
    # filtran si no hay movimiento de precio ni catalizador real.
    if source == "SEC_EDGAR":
        article["edgar_bypass"] = True
        logger.debug(f"[EDGAR-BYPASS] ticker={tickers_in_text} score={score} — pasa sin keyword check")
    elif score < 3:
        return False, f"score bajo ({score}/10) — keywords insuficientes"

    # 5. Anotar señales adicionales (no descartan, pero informan a Claude)
    title_upper = article.get("title", "").upper()
    if any(kw in title_upper for kw in PRICED_HEADLINE_WORDS):
        article["possible_already_priced"] = True

    if any(kw in raw_text for kw in WEAK_KEYWORDS):
        article["has_weak_signals"] = True

    return True, None


def ticker_in_text(ticker: str, text_upper: str) -> bool:
    """Comprueba que el ticker aparece como palabra completa (no dentro de otra)."""
    return bool(re.search(r'\b' + re.escape(ticker.upper()) + r'\b', text_upper))


def extract_tickers_from_text(text: str, watchlist: list[str]) -> list[str]:
    text_upper = text.upper()
    return [t for t in watchlist if ticker_in_text(t, text_upper)]
