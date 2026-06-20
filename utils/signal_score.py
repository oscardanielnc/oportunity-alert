"""
signal_score — NUEVO score de acción (reemplaza el score de convicción como guía de decisión).

El estudio 2026-06-20 demostró que el score de convicción (0-7) NO predice la reacción
(dir_ok ~52%, magnitud ~0). Lo que SÍ importa: CATEGORÍA del catalizador + FRESCURA + si el
PRECIO ya confirma. Este módulo calcula un score 0-10 con esos 3 componentes y REUTILIZA el
campo `conviction_score` existente (cero cambios de esquema; solo cambia QUIÉN lo alimenta).

  score = categoria(0-6) + frescura(0-2) + confirmacion_precio(0-2)

Las prioridades de categoría salen del backfill (catalyst_reactions): contratos/gobierno, FDA,
ofertas/dilución, leases, partnerships = alto; PT-maintains, votación 5.07, sector = ~0 (de hecho
ya se filtran antes de la IA). Se afinará con el scoreboard forward (brazo de auditoría).
"""
from __future__ import annotations
import re

# Categoría -> puntos (0-6). Basado en reacción anormal + frescura + % no-priceado del backfill.
CATEGORY_POINTS = {
    "contract_govt":     6,
    "fda_clinical":      6,
    "offering_dilution": 5,   # dilución mueve fuerte (bajista)
    "product_partner":   5,   # incluye leases de data center, MOUs con socio relevante
    "ma_rumor":          4,
    "sec_regfd_7_01":    3,
    "earnings_guidance": 3,   # a menudo ya priceado
    "analyst_upgrade":   3,   # upgrade REAL (cambio de rating), no PT-maintains
    "sec_other_8k":      2,
    "otro":              2,
    "sector_momentum":   1,
    "analyst_maintain":  0,   # PT sin cambio de rating — ruido
    "sec_vote_5_07":     0,   # trámite — ya se filtra
}

# Clasificador de categoría por keywords sobre resumen+titular+url (versión producción del de research).
_RULES = [
    ("fda_clinical",      [r"\bfda\b", r"approval", r"\bpdufa\b", r"clinical", r"phase [123]", r"aprobaci[oó]n"]),
    ("contract_govt",     [r"contract", r"contrato", r"\bdod\b", r"pentagon", r"defens", r"\baward", r"adjudicaci", r"chips act", r"\bgobierno\b", r"\barmy\b|\bnavy\b|air force|afrl|nasa"]),
    ("offering_dilution", [r"offering", r"oferta p[uú]blica", r"diluci[oó]n", r"prospectus", r"underwritten", r"convertible", r"registered direct", r"\bshelf\b", r"shares of common"]),
    ("ma_rumor",          [r"\bm&a\b", r"acquisition", r"adquisici", r"\bmerger\b", r"takeover", r"\bbuyout\b", r"in talks", r"\brumor"]),
    ("product_partner",   [r"data center lease", r"\blease\b", r"memorandum of understanding", r"\bmou\b", r"partnership", r"alianza", r"collaborat", r"colaboraci", r"\blaunch", r"unveil", r"hyperscaler"]),
    ("earnings_guidance", [r"earnings", r"\beps\b", r"\brevenue\b", r"\bbeat\b", r"\bmiss\b", r"guidance", r"quarterly", r"\bq[1-4]\b"]),
    ("analyst_upgrade",   [r"upgraded to (buy|overweight|outperform)", r"raised to buy", r"upgrades to (buy|overweight)"]),
    ("analyst_maintain",  [r"price target", r"\bpt\b", r"maintains|reitera|reaffirm", r"overweight|underweight|neutral|outperform"]),
    ("sec_vote_5_07",     [r"item 5\.07", r"votaci[oó]n|shareholder|accionistas|annual meeting"]),
    ("sec_regfd_7_01",    [r"item 7\.01", r"regulation fd|reg fd"]),
    ("sec_other_8k",      [r"8-k", r"\bitem \d"]),
    ("sector_momentum",   [r"sectorial", r"\bsector\b", r"arrastra", r"m[aá]ximos de 52", r"52[- ]week"]),
]


def classify_category(text: str) -> str:
    t = (text or "").lower()
    for name, pats in _RULES:
        if any(re.search(p, t) for p in pats):
            return name
    return "otro"


def freshness_points(age_minutes) -> int:
    try:
        a = float(age_minutes)
    except (TypeError, ValueError):
        return 1
    if a <= 15:
        return 2
    if a <= 60:
        return 1
    return 0


def price_confirm_points(change_pct, direction) -> int:
    """¿El precio YA se mueve en la dirección predicha? 2=confirma, 1=neutral, 0=en contra."""
    try:
        ch = float(change_pct)
    except (TypeError, ValueError):
        return 1
    d = (direction or "").upper()
    if d == "LONG":
        return 2 if ch >= 1.0 else (0 if ch <= -1.0 else 1)
    if d == "SHORT":
        return 2 if ch <= -1.0 else (0 if ch >= 1.0 else 1)
    return 1


def compute_signal_score(category: str, age_minutes, change_pct, direction) -> dict:
    """Devuelve {score 0-10, cat_pts, fresh_pts, conf_pts, categoria}. Reutiliza conviction_score."""
    cat_pts = CATEGORY_POINTS.get(category, 2)
    fresh = freshness_points(age_minutes)
    conf = price_confirm_points(change_pct, direction)
    return {"score": cat_pts + fresh + conf, "categoria": category,
            "cat_pts": cat_pts, "fresh_pts": fresh, "conf_pts": conf}


# ── Regla de SMS (umbral de envío) ───────────────────────────────────────────
# Oscar: enviar todo lo "bueno"; suprimir lo de <1% de impacto esperado. Como aún no podemos
# predecir el % por-noticia (eso lo aprende el scoreboard forward), se aproxima por CATEGORÍA:
# las categorías con impacto histórico ~0 (PT-maintains, sector, votación) puntúan bajo y no
# alcanzan el umbral. Cuando el scoreboard acumule impacto real por categoría, se reemplaza el
# umbral por "MFE esperado >= 1%".
SMS_SCORE_THRESHOLD = 6   # >=6 => categoría decente, o fresca+confirmada por precio


def should_send_sms(score_detail: dict) -> bool:
    return score_detail["score"] >= SMS_SCORE_THRESHOLD
