"""
Capa 3: Scoring con IA.
Motor primario: Gemini 2.0 Flash (gratis hasta 1,500 req/día).
Motor secundario: Claude (si se configura ANTHROPIC_API_KEY).
Selección via AI_ENGINE env var: "gemini" | "claude"

v2.0: acepta parámetro conviction (dict de conviction_gates).
  - Prompt reducido ~50% — IA ya NO calcula stops/targets.
  - Stops y targets los inyecta post-procesamiento desde conviction.

v2.1: motor unificado via utils.ai_client.call_ai() — sin código Gemini/Claude duplicado.
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta

from utils.alpaca_price import summarize_candles
from utils.ai_client import call_ai, parse_json_response

logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _calc_exit_date(horizonte: str) -> str:
    """Convierte horizonte textual a fecha/hora Lima."""
    now = datetime.now(LIMA)
    h = horizonte.lower()
    if "mismo dia" in h:
        exit_dt = now.replace(hour=14, minute=45, second=0, microsecond=0)
    elif "day+1" in h:
        exit_dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "3-5" in h:
        exit_dt = (now + timedelta(days=4)).replace(hour=14, minute=0, second=0, microsecond=0)
    else:
        exit_dt = (now + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)
    return exit_dt.strftime("%Y-%m-%d %I:%M%p Lima")


def _sector_context(ticker: str) -> tuple[str, int]:
    """Retorna (sector_context, priced_threshold)."""
    QUANTUM    = {"QBTS", "IONQ", "RGTI", "QUBT"}
    SMALL_AI   = {"SOUN", "BBAI", "INOD", "APLD", "IREN"}
    SPACE      = {"RKLB", "LUNR", "JOBY", "ACHR"}
    BTC_MINERS = {"MARA", "RIOT", "MSTR"}
    MEME       = {"GME", "AMC"}
    HIGHVOL    = {"NVDA", "TSLA", "SMCI", "RIVN", "LCID", "WOLF", "PLTR", "APP"}

    if ticker in QUANTUM:
        return (
            f"{ticker} es computacion cuantica — movimientos de 30-150% en 1-3 dias son normales. "
            f"Umbral priceado: 30%.", 30,
        )
    if ticker in BTC_MINERS:
        return (
            f"{ticker} es minero de Bitcoin — puede subir 40-80% en un dia con catalizador. "
            f"Umbral priceado: 25%.", 25,
        )
    if ticker in SMALL_AI:
        return (
            f"{ticker} es AI de pequena cap — reacciona fuerte a contratos gobierno. "
            f"Movimientos 30-100% en horas. Umbral priceado: 25%.", 25,
        )
    if ticker in SPACE:
        return (
            f"{ticker} es espacio/eVTOL — contratos DoD o hitos produccion mueven 20-60%. "
            f"Umbral priceado: 20%.", 20,
        )
    if ticker in MEME:
        return (
            f"{ticker} es meme stock — squeezes pueden ir 50-300% en horas. Umbral priceado: 30%.",
            30,
        )
    if ticker in HIGHVOL:
        return (
            f"{ticker} es accion alta volatilidad — puede mover 15-40% ante catalizadores. "
            f"Umbral priceado: 15%.", 15,
        )
    return (f"{ticker} es accion de volatilidad moderada-alta. Umbral priceado: 10%.", 10)


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_prompt(
    article: dict,
    price_data: dict,
    conviction: dict = None,
    context_text: str = "",
) -> tuple[str, str]:
    """Retorna (system_prompt, user_prompt). context_text = contexto calculado por código."""
    ticker      = (article.get("tickers_found") or ["UNKNOWN"])[0]
    title       = article.get("title", "")
    summary     = article.get("summary", "")
    age_minutes = article.get("age_minutes", 0)
    source      = article.get("source", "Desconocida")

    current_price  = price_data.get("current_price")
    change_pct     = price_data.get("change_pct")
    last_trade_age = price_data.get("last_trade_age_minutes")
    price_error    = price_data.get("error")

    sector_ctx, priced_threshold = _sector_context(ticker)

    price_warning = ""
    if price_error:
        price_warning = f"AVISO: {price_error[:80]}"
    elif last_trade_age and last_trade_age > 10:
        price_warning = f"AVISO: Precio con {last_trade_age:.0f} min de antigüedad — mercado cerrado"

    breaking_note = ""
    if age_minutes < 5:
        breaking_note = (
            f"BREAKING (<{age_minutes:.0f} min): solo descartar si precio ya supero {priced_threshold}%. "
            f"El movimiento inicial no agota el catalizador."
        )

    conv_str = ""
    if conviction:
        rsi   = conviction.get("gate2_rsi", 0)
        ema20 = conviction.get("gate2_ema20", 0)
        atr   = conviction.get("atr14", 0)
        score = conviction.get("conviction_score", 0)
        conv_str = (
            f"\nCONVICCION TECNICA (ya calculada — no analizar de nuevo):\n"
            f"  Score: {score}/7 | RSI14: {rsi:.1f} | EMA20: ${ema20:.2f} | ATR14: ${atr:.2f}\n"
        )

    is_continuation = bool(conviction and conviction.get("momentum_continuation"))
    context_block = f"\nCONTEXTO (calculado por código — úsalo, no lo recalcules):\n{context_text}\n" if context_text else ""

    continuation_note = ""
    if is_continuation:
        continuation_note = (
            "\n⚠️ CONTINUACIÓN: la acción YA tuvo un movimiento grande con este catalizador. "
            "NO la trates como entrada fresca. Decide si queda recorrido de CORTO PLAZO "
            "(continuación de 1-2 días antes de revertir) o si el catalizador ya se agotó. "
            "Si queda algo de recorrido pon score 5-6 (ADVERTENCIA, no oportunidad limpia) y "
            "en 'ventana' indica los días. Si ya se agotó, score ≤4.\n"
        )

    system_prompt = (
        "Eres el analista de mercado de Oscar (25 años, ingeniero, ~$6,000 en eToro, busca "
        "movimientos de 15-100% en 1-5 días, opera LONG y SHORT 24h). Tu trabajo tiene DOS partes:\n"
        "1) CLASIFICAR el catalizador con score_ia 1-10 y dirección.\n"
        "2) Analizar el CONTEXTO que el código NO puede calcular: ¿el sector vive un buen "
        "momento o enfrenta vientos en contra (prohibiciones, guerra, regulación, sector saturado)? "
        "¿Hay catalizadores macro que REFUERCEN o ANULEN esta noticia? Una buena noticia en un "
        "sector en caída puede no mover el precio; una mediana en un sector caliente sí.\n"
        "Además TRADUCE la noticia a lenguaje simple: sin códigos ni jerga (ej. quita '8-K Item 3.02'), "
        "que cualquiera entienda QUÉ pasó y SI es bueno o malo.\n"
        "NO calcules stops ni targets (los calcula el código por ATR). "
        "Responde SOLO con JSON válido, sin texto adicional, sin markdown."
    )

    user_prompt = f"""EVALUA ESTE CATALIZADOR:

Ticker: {ticker} | Precio: ${current_price if current_price else 'N/D'} | Cambio: {f'{change_pct:+.1f}%' if change_pct is not None else 'N/D'}
Noticia: hace {age_minutes:.0f} min | Fuente: {source}
{price_warning}
{conv_str}{context_block}
Sector: {sector_ctx}
{breaking_note}{continuation_note}

Titulo: {title}
Resumen: {summary[:450]}

RESPONDE SOLO con este JSON (entrada/stop/target los agrega el codigo — no los calcules):
{{
  "ticker": "{ticker}",
  "score_ia": 0,
  "tipo_catalizador": "DIRECTO|SECTORIAL|RUMOR",
  "direccion": "LONG|SHORT",
  "catalizador_priceado": false,
  "titular_simple": "el titular en lenguaje claro, sin codigos ni jerga (max 12 palabras)",
  "analisis_simple": "1-2 frases que cualquiera entienda: que paso y si es bueno o malo para la accion y por que",
  "contexto_sector": "como esta el sector/macro y si refuerza o debilita esta noticia (1 linea)",
  "resumen_cataliz": "descripcion tecnica en 1 linea del catalizador",
  "ventana": "vacio normalmente; si es CONTINUACION indica dias (ej. '1-2 dias antes de revertir')",
  "timing_entrada": "ahora AH | apertura 8:30am Lima | esperar pullback a $X",
  "horizonte_tiempo": "mismo dia | Day+1 | 3-5 dias",
  "riesgo": "descripcion del riesgo principal en 1 linea"
}}

score_ia — escala ESTRICTA del 1 al 10 (entero), PONDERANDO el contexto sectorial/macro:
  9-10: catalizador transformacional directo, no priceado, direccion inequivoca, sector a favor
  7-8 : catalizador claro y nuevo, buena certeza de direccion, contexto no lo contradice
  5-6 : catalizador real pero con dudas (impacto, timing, ya priceado, o sector en contra)
  3-4 : señal debil/ambigua, o el contexto macro/sectorial anula el efecto de la noticia
  1-2 : especulativo, sin evidencia solida, o precio ya movio lo que correspondia"""

    return system_prompt, user_prompt


# ── Función principal ─────────────────────────────────────────────────────────

def score_with_claude(
    article: dict,
    price_data: dict,
    model: str = None,
    conviction: dict = None,
    context_text: str = "",
) -> dict:
    """
    Punto de entrada principal. Delega la llamada IA a utils.ai_client.call_ai().
    conviction: dict de evaluate_conviction() — si se pasa, el prompt es más corto
    y el post-procesamiento rellena stops/targets/conviction_score desde código.
    context_text: contexto calculado por código (utils.news_context) — sector/macro,
    trayectoria, noticias previas. Lo analiza la IA pero NO gasta tokens calculándolo.
    """
    ai_engine = os.environ.get("AI_ENGINE", "gemini").lower()
    ticker    = (article.get("tickers_found") or ["UNKNOWN"])[0]

    system_prompt, user_prompt = _build_prompt(article, price_data, conviction, context_text)

    logger.info(f"[Scorer] {ai_engine.capitalize()} — {ticker}")

    raw = call_ai(
        user_prompt,
        system=system_prompt,
        max_tokens=768,   # +campos de lenguaje simple + contexto sectorial + ventana
        engine=ai_engine,
    )

    result = parse_json_response(raw)
    if result is None:
        msg = "sin respuesta de IA" if raw is None else f"JSON invalido: {str(raw)[:60]}"
        logger.error(f"[Scorer] {ticker}: {msg}")
        return _error_result(article, ticker, msg)

    # Derivar prioridad y pct_estimado desde código (no desde IA)
    score_ia = int(result.get("score_ia") or 0)
    result["score_ia"] = score_ia
    result["prioridad"] = _score_to_prioridad(score_ia)

    # Campos de lenguaje simple + contexto (defaults si la IA los omite)
    result.setdefault("titular_simple", result.get("resumen_cataliz", ""))
    result.setdefault("analisis_simple", "")
    result.setdefault("contexto_sector", "")
    result.setdefault("ventana", "")
    # Tipo de alerta: continuación de un movimiento ya priceado = ADVERTENCIA (no oportunidad limpia)
    if conviction and conviction.get("momentum_continuation"):
        result["tipo_alerta"] = "ADVERTENCIA"
    else:
        result["tipo_alerta"] = "OPORTUNIDAD"

    # Post-procesamiento: inyectar stops/targets/convicción calculados por código
    if conviction:
        entry  = conviction.get("entry_price", 0)
        stop   = conviction.get("stop_code", 0)
        target = conviction.get("target_code", 0)
        result["entrada_rango"]     = f"${entry:.2f}" if entry  > 0 else "N/A"
        result["stop"]              = f"${stop:.2f}"   if stop   > 0 else "N/A"
        result["target"]            = f"${target:.2f}" if target > 0 else "N/A"
        result["conviction_score"]  = conviction.get("conviction_score")
        result["gate_detail"]       = conviction.get("reasoning", "")
        result["salida_fecha"]      = _calc_exit_date(result.get("horizonte_tiempo", "3-5 dias"))
        result["salida_anticipada"] = (
            f"Salir si baja de ${stop:.2f} (stop ATR) "
            f"o RSI > 78 sin confirmación de volumen"
        )
        # pct_estimado calculado por código desde target ATR (más confiable que estimado IA)
        if entry > 0 and target > 0:
            direction = result.get("direccion", "LONG")
            raw_pct = (target - entry) / entry * 100 if direction == "LONG" else (entry - target) / entry * 100
            result["pct_estimado"] = round(abs(raw_pct), 1)
        else:
            result["pct_estimado"] = 0.0
    else:
        result.setdefault("entrada_rango",    f"${price_data.get('current_price', 0):.2f}")
        result.setdefault("stop",             "N/A")
        result.setdefault("target",           "N/A")
        result.setdefault("salida_fecha",     _calc_exit_date(result.get("horizonte_tiempo", "3-5 dias")))
        result.setdefault("salida_anticipada", "Ver análisis")
        result.setdefault("pct_estimado", 0.0)

    # Metadatos
    result["article_id"]         = article.get("id", "")
    result["article_url"]        = article.get("url", "")
    result["source"]             = article.get("source", "")
    result["age_minutes"]        = article.get("age_minutes", 0)
    result["precio_al_alerta"]   = price_data.get("current_price")
    result["precio_24h_despues"] = None
    result["timestamp"]          = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-05:00")
    result["ai_engine"]          = ai_engine

    return result


def _score_to_prioridad(score: int) -> str:
    if score >= 7:
        return "ALTA"
    if score >= 4:
        return "MEDIA"
    if score >= 1:
        return "BAJA"
    return "ERROR"


def _error_result(article: dict, ticker: str, error_msg: str) -> dict:
    return {
        "ticker":              ticker,
        "score_ia":            0,
        "prioridad":           "ERROR",
        "tipo_catalizador":    "desconocido",
        "direccion":           "N/A",
        "pct_estimado":        0.0,
        "catalizador_priceado": False,
        "resumen_cataliz":     f"Error en scoring: {error_msg}",
        "entrada_rango":       "N/A",
        "stop":                "N/A",
        "target":              "N/A",
        "timing_entrada":      "N/A",
        "horizonte_tiempo":    "N/A",
        "salida_fecha":        "N/A",
        "salida_anticipada":   "N/A",
        "riesgo":              "Error en análisis",
        "article_id":          article.get("id", ""),
        "article_url":         article.get("url", ""),
        "source":              article.get("source", ""),
        "age_minutes":         article.get("age_minutes", 0),
        "precio_al_alerta":    None,
        "precio_24h_despues":  None,
        "timestamp":           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-05:00"),
        "ai_engine":           os.environ.get("AI_ENGINE", "gemini"),
        "error":               error_msg,
    }
