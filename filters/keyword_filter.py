"""
Capa 2: Filtro rápido Python (<5ms por noticia).
Tres niveles de keyword para minimizar llamadas a Claude:
  TIER_1: 1 sola keyword ya justifica llamar a Claude (eventos de alto impacto)
  TIER_2: necesita 2+ keywords, o 1 keyword + señales de refuerzo
  WEAK:   degradan score, no descartan solos
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
    "GOVERNMENT STAKE", "EQUITY STAKE",
    # FDA
    "FDA APPROVAL", "FDA APPROVED", "FDA REJECTED", "FDA CLEARANCE",
    "NDA APPROVAL", "BLA APPROVAL", "PDUFA",
    # M&A
    "ACQUISITION", "MERGER AGREEMENT", "TAKEOVER BID", "BUYOUT OFFER",
    "TO BE ACQUIRED", "GOING PRIVATE", "TENDER OFFER",
    # Earnings directos
    "BEATS ESTIMATES", "EPS BEAT", "REVENUE BEAT", "BEATS EXPECTATIONS",
    "RAISES GUIDANCE", "GUIDANCE RAISED", "RECORD REVENUE",
    # Short squeeze
    "SHORT SQUEEZE", "GAMMA SQUEEZE",
]

# ── Tier 2: necesita 2+ hits para pasar a Claude ──────────────────────────────
# Señales reales pero más comunes — solas pueden ser ruido
TIER_2_KEYWORDS = [
    "GRANT", "BILLION AWARD", "MILLION AWARD",
    "DEPARTMENT OF COMMERCE", "DEFENSE DEPARTMENT",
    "CLINICAL TRIAL RESULTS", "PHASE 3", "PHASE 2",
    "STRATEGIC REVIEW", "MERGER", "JOINT VENTURE",
    "PRICE TARGET RAISED", "UPGRADED TO BUY", "UPGRADED TO OVERWEIGHT",
    "STRONG BUY", "INITIATED COVERAGE", "OUTPERFORM",
    "INVESTOR DAY", "ANALYST DAY", "SPECIAL DIVIDEND",
    "SHARE BUYBACK", "STOCK BUYBACK",
    "PARTNERSHIP ANNOUNCED", "STRATEGIC PARTNERSHIP",
    "INSIDER BUYING", "CEO PURCHASED", "CFO PURCHASED",
    "SECURES CONTRACT", "WINS CONTRACT", "SELECTED BY",
    "MEMORANDUM OF UNDERSTANDING",
    "UNUSUAL OPTIONS",
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


def passes_filter(
    article: dict,
    watchlist: list[str],
    seen_ids: set,
    max_age_minutes: int = 45,
) -> tuple[bool, Optional[str]]:
    """
    Retorna (True, None) si pasa el filtro, o (False, razón) si no pasa.

    Score mínimo para pasar:
      - Tier 1 solo (score >= 4): pasa siempre
      - Tier 2 con 2+ keywords (score >= 4): pasa
      - Score < 3: no llega a Claude (ahorra API calls)
    """
    article_id = article.get("id", "")
    raw_text = article.get("raw_text", "").upper()
    age = article.get("age_minutes", 999)

    # 1. Deduplicación por ID
    if article_id in seen_ids:
        return False, "duplicado"

    # 2. Edad máxima
    if age > max_age_minutes:
        return False, f"demasiado vieja ({age:.0f} min)"

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

    if score < 3:
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
