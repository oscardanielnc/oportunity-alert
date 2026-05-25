"""
Event Gate v2.1 — manejo de earnings, breaking news y noticias bomba.

En eventos extraordinarios los indicadores técnicos históricos (RSI, EMA) son
irrelevantes — el precio reacciona a información nueva, no a patrones pasados.
Este módulo reemplaza Gate 2 con análisis de velas 1m en tiempo real.
"""
import logging

logger = logging.getLogger(__name__)

# ── Detección de tipo de evento ───────────────────────────────────────────────

EARNINGS_KEYWORDS = {
    "earnings", "eps", "revenue", "quarterly", "q1", "q2", "q3", "q4",
    "beat", "miss", "guidance", "outlook", "forecast", "results",
    "per share", "net income", "profit", "loss", "raises guidance",
    "lowers guidance", "fiscal", "quarter",
}
FDA_KEYWORDS = {
    "fda", "approval", "approved", "pdufa", "nda", "bla", "anda",
    "clinical trial", "phase 3", "phase 2", "breakthrough therapy",
    "complete response", "rejection", "advisory committee",
}
GOV_CONTRACT_KEYWORDS = {
    "dod", "department of defense", "contract", "award", "chips act",
    "government", "pentagon", "darpa", "nasa", "defense", "grant",
    "procurement", "billion", "million award",
}
BREAKING_AGE_THRESHOLD = 4  # minutos — noticia <4 min = breaking


def detect_event_type(article: dict, price_data: dict) -> str:
    """
    Clasifica el tipo de evento para decidir si usar Event Mode.

    Retorna: "earnings" | "fda" | "government_contract" |
             "breaking_news" | "large_move" | "normal"
    """
    text = (
        (article.get("title", "") + " " + article.get("summary", ""))
        .lower()
    )
    age = article.get("age_minutes", 999)
    change_pct = abs(price_data.get("change_pct") or 0)

    # Prioridad: earnings > FDA > contrato > breaking > large move
    if any(kw in text for kw in EARNINGS_KEYWORDS):
        return "earnings"
    if any(kw in text for kw in FDA_KEYWORDS):
        return "fda"
    if any(kw in text for kw in GOV_CONTRACT_KEYWORDS):
        return "government_contract"
    if age < BREAKING_AGE_THRESHOLD:
        return "breaking_news"
    if change_pct > 10:
        return "large_move"
    return "normal"


def is_event_mode(event_type: str) -> bool:
    """True para todos los tipos que requieren bypass de Gate 2 técnico."""
    return event_type != "normal"


# ── Análisis de estabilización con velas 1m ───────────────────────────────────

def analyze_stabilization(candles_1m: list, direction: str = "LONG") -> dict:
    """
    Analiza si el precio se está estabilizando después del spike inicial.
    Las velas vienen en orden DESC (candles[0] = más reciente).

    Retorna:
    {
        "is_stable": bool,
        "confidence": "ALTA" | "MEDIA" | "BAJA",
        "signal": "ENTRAR_AHORA" | "ESPERAR_CONFIRMACION" | "DESCARTAR" | "SIN_DATOS",
        "reasoning": str,
        "volume_sustained": bool,
        "range_narrowing": bool,
        "higher_lows": bool,      # LONG
        "lower_highs": bool,      # SHORT
        "event_gate_score": int,  # 0-3 (reemplaza gate2_score en event mode)
    }
    """
    base = {
        "is_stable": False,
        "confidence": "BAJA",
        "signal": "SIN_DATOS",
        "reasoning": "Sin velas disponibles",
        "volume_sustained": False,
        "range_narrowing": False,
        "higher_lows": False,
        "lower_highs": False,
        "event_gate_score": 1,  # score neutro cuando no hay datos
    }

    if not candles_1m or len(candles_1m) < 3:
        base["reasoning"] = f"Solo {len(candles_1m)} velas — datos insuficientes para análisis"
        base["signal"] = "ESPERAR_CONFIRMACION"
        return base

    # Ordenar para garantizar DESC (más reciente primero)
    candles = sorted(candles_1m, key=lambda c: c.get("t", ""), reverse=True)
    recent = candles[:3]   # últimas 3 velas
    older  = candles[3:6]  # 3 velas anteriores (si existen)

    # ── 1. Volumen sostenido ──────────────────────────────────────────────────
    recent_vols = [c.get("v", 0) for c in recent if c.get("v", 0) > 0]
    older_vols  = [c.get("v", 0) for c in older  if c.get("v", 0) > 0]

    if recent_vols and older_vols:
        avg_recent = sum(recent_vols) / len(recent_vols)
        avg_older  = sum(older_vols)  / len(older_vols)
        volume_sustained = avg_recent >= avg_older * 0.7  # tolerar 30% caída
    elif recent_vols:
        volume_sustained = True  # sin histórico, asumir ok
    else:
        volume_sustained = False

    # ── 2. Rango achicando (volatilidad decreciente = estabilización) ─────────
    recent_ranges = [c.get("h", 0) - c.get("l", 0) for c in recent if c.get("h") and c.get("l")]
    older_ranges  = [c.get("h", 0) - c.get("l", 0) for c in older  if c.get("h") and c.get("l")]

    if recent_ranges and older_ranges:
        avg_recent_range = sum(recent_ranges) / len(recent_ranges)
        avg_older_range  = sum(older_ranges)  / len(older_ranges)
        range_narrowing  = avg_recent_range < avg_older_range * 0.85
    else:
        range_narrowing = False

    # ── 3. Dirección confirmada ───────────────────────────────────────────────
    lows  = [c.get("l", 0) for c in candles[:4] if c.get("l")]
    highs = [c.get("h", 0) for c in candles[:4] if c.get("h")]

    higher_lows  = False
    lower_highs  = False

    if len(lows) >= 3:
        # Lows en orden temporal (más antiguo primero para verificar tendencia)
        lows_asc = list(reversed(lows))
        higher_lows = all(lows_asc[i] <= lows_asc[i + 1] for i in range(len(lows_asc) - 1))

    if len(highs) >= 3:
        highs_asc = list(reversed(highs))
        lower_highs = all(highs_asc[i] >= highs_asc[i + 1] for i in range(len(highs_asc) - 1))

    direction_ok = higher_lows if direction == "LONG" else lower_highs

    # ── Score y señal final ───────────────────────────────────────────────────
    score = int(volume_sustained) + int(range_narrowing) + int(direction_ok)

    if score >= 3:
        signal     = "ENTRAR_AHORA"
        confidence = "ALTA"
        is_stable  = True
    elif score == 2:
        signal     = "ENTRAR_AHORA"
        confidence = "MEDIA"
        is_stable  = True
    elif score == 1:
        signal     = "ESPERAR_CONFIRMACION"
        confidence = "BAJA"
        is_stable  = False
    else:
        signal     = "DESCARTAR"
        confidence = "BAJA"
        is_stable  = False

    # Construir reasoning legible
    parts = []
    parts.append(f"Vol: {'sostenido' if volume_sustained else 'cayendo'}")
    parts.append(f"Rango: {'achicando' if range_narrowing else 'amplio'}")
    if direction == "LONG":
        parts.append(f"Minimos: {'crecientes' if higher_lows else 'sin patron'}")
    else:
        parts.append(f"Maximos: {'decrecientes' if lower_highs else 'sin patron'}")
    reasoning = f"{score}/3 — {' | '.join(parts)}"

    return {
        "is_stable":        is_stable,
        "confidence":       confidence,
        "signal":           signal,
        "reasoning":        reasoning,
        "volume_sustained": volume_sustained,
        "range_narrowing":  range_narrowing,
        "higher_lows":      higher_lows,
        "lower_highs":      lower_highs,
        "event_gate_score": score,
    }


# ── Gate 2 adaptado para eventos ──────────────────────────────────────────────

def event_gate2_score(candles_1m: list, direction: str = "LONG") -> int:
    """
    Reemplaza gate2_score en event mode.
    Retorna 0-3 basado en estabilización (igual escala que Gate 2 normal).
    """
    stab = analyze_stabilization(candles_1m, direction)
    return stab["event_gate_score"]


# ── Formato del SMS 1 (aviso inmediato) ──────────────────────────────────────

EVENT_LABELS = {
    "earnings":           "EARNINGS",
    "fda":                "FDA",
    "government_contract": "CONTRATO GOB",
    "breaking_news":      "BREAKING NEWS",
    "large_move":         "MOVIMIENTO FUERTE",
}


def format_event_watch_sms(
    ticker: str,
    event_type: str,
    result: dict,
    price_data: dict,
    followup_minutes: int = 7,
) -> str:
    """Formato del SMS 1: aviso de evento, SIN señal de entrada."""
    label     = EVENT_LABELS.get(event_type, "EVENTO")
    direccion = result.get("direccion", "?")
    pct       = result.get("pct_estimado", 0)
    price     = price_data.get("current_price")
    change    = price_data.get("change_pct")
    cataliz   = result.get("resumen_cataliz", "")[:120]
    prioridad = result.get("prioridad", "?")

    price_str  = f"${price:.2f}" if price else "N/D"
    change_str = f"{change:+.1f}%" if change is not None else "N/D"
    pct_str    = f" (~+{pct:.0f}% potencial)" if pct else ""

    return (
        f"[EVENTO] {label} — {ticker} [{direccion}]\n"
        f"{cataliz}\n"
        f"\n"
        f"Precio ahora: {price_str} ({change_str}){pct_str}\n"
        f"Prioridad preliminar: {prioridad}\n"
        f"\n"
        f"MODO EVENTO — No entrar aun\n"
        f"Spike inicial en curso. Re-evaluacion automatica en {followup_minutes} min.\n"
        f"Si no llega el segundo mensaje: precio no confirmo."
    )


def format_event_confirm_sms(
    ticker: str,
    event_type: str,
    result: dict,
    price_data: dict,
    stab: dict,
    original_price: float,
) -> str:
    """Formato del SMS 2: confirmación o descarte tras la estabilización."""
    label     = EVENT_LABELS.get(event_type, "EVENTO")
    direccion = result.get("direccion", "?")
    price     = price_data.get("current_price")
    signal    = stab.get("signal", "SIN_DATOS")
    confidence = stab.get("confidence", "BAJA")
    reasoning  = stab.get("reasoning", "")
    entry      = result.get("entrada_rango", "N/D")
    stop       = result.get("stop", "N/D")
    target     = result.get("target", "N/D")
    prioridad  = result.get("prioridad", "?")
    conviction = result.get("conviction_score", "?")

    price_str = f"${price:.2f}" if price else "N/D"
    move_str  = ""
    if price and original_price:
        move = (price - original_price) / original_price * 100
        move_str = f" ({move:+.1f}% desde alerta 1)"

    if signal in ("ENTRAR_AHORA",):
        action_line = f"ENTRADA VALIDA — {entry}"
        detail_line = f"Stop: {stop} | Target: {target}"
    elif signal == "ESPERAR_CONFIRMACION":
        action_line = f"ESPERAR — precio aun no confirmo"
        detail_line = f"Zona de entrada si cierra sobre {entry}"
    else:  # DESCARTAR o SIN_DATOS
        action_line = "DESCARTAR — no entrar"
        detail_line = "Precio no confirmo direccion o volumen murio"

    return (
        f"[RE-EVAL {label}] {ticker} [{direccion}]\n"
        f"\n"
        f"Precio: {price_str}{move_str}\n"
        f"Estabilizacion: {reasoning}\n"
        f"Señal: {signal} ({confidence})\n"
        f"\n"
        f"{action_line}\n"
        f"{detail_line}\n"
        f"\n"
        f"Conviccion: {conviction}/7 | Prioridad: {prioridad}"
    )
