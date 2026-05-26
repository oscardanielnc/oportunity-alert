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
) -> tuple[str, str]:
    """Retorna (system_prompt, user_prompt). Con conviction, el prompt es ~50% más corto."""
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

    system_prompt = (
        "Eres el sistema de alertas de trading de Oscar, 25 años, ingeniero, capital $6,000 en eToro. "
        "Oscar busca movimientos de 15-100%+ en 1-5 días. Opera LONG y SHORT en eToro 24h. "
        "Tu trabajo: DETECTAR y CLASIFICAR catalizadores. NO calcules stops ni targets — "
        "los calcula el código por ATR. Ante duda entre MEDIA y BAJA → usa MEDIA. "
        "Responde SOLO con JSON válido, sin texto adicional, sin markdown."
    )

    user_prompt = f"""EVALUA ESTE CATALIZADOR:

Ticker: {ticker} | Precio: ${current_price if current_price else 'N/D'} | Cambio: {f'{change_pct:+.1f}%' if change_pct is not None else 'N/D'}
Noticia: hace {age_minutes:.0f} min | Fuente: {source}
{price_warning}
{conv_str}
Sector: {sector_ctx}
{breaking_note}

Titulo: {title}
Resumen: {summary[:450]}

RESPONDE SOLO con este JSON (entrada/stop/target los agrega el codigo — no los calcules):
{{
  "ticker": "{ticker}",
  "prioridad": "ALTA|MEDIA|BAJA|DESCARTADO",
  "tipo_catalizador": "DIRECTO|SECTORIAL|RUMOR",
  "direccion": "LONG|SHORT",
  "pct_estimado": 0.0,
  "catalizador_priceado": false,
  "resumen_cataliz": "descripcion en 1 linea del catalizador",
  "timing_entrada": "ahora AH | apertura 8:30am Lima | esperar pullback a $X",
  "horizonte_tiempo": "mismo dia | Day+1 | 3-5 dias",
  "riesgo": "descripcion del riesgo principal en 1 linea",
  "confianza": "ALTA|MEDIA|BAJA"
}}"""

    return system_prompt, user_prompt


# ── Función principal ─────────────────────────────────────────────────────────

def score_with_claude(
    article: dict,
    price_data: dict,
    model: str = None,
    conviction: dict = None,
) -> dict:
    """
    Punto de entrada principal. Delega la llamada IA a utils.ai_client.call_ai().
    conviction: dict de evaluate_conviction() — si se pasa, el prompt es más corto
    y el post-procesamiento rellena stops/targets/conviction_score desde código.
    """
    ai_engine = os.environ.get("AI_ENGINE", "gemini").lower()
    ticker    = (article.get("tickers_found") or ["UNKNOWN"])[0]

    system_prompt, user_prompt = _build_prompt(article, price_data, conviction)

    logger.info(f"[Scorer] {ai_engine.capitalize()} — {ticker}")

    raw = call_ai(
        user_prompt,
        system=system_prompt,
        max_tokens=512,
        engine=ai_engine,
    )

    result = parse_json_response(raw)
    if result is None:
        msg = "sin respuesta de IA" if raw is None else f"JSON invalido: {str(raw)[:60]}"
        logger.error(f"[Scorer] {ticker}: {msg}")
        return _error_result(article, ticker, msg)

    # Post-procesamiento: inyectar stops/targets/convicción calculados por código
    if conviction:
        entry  = conviction.get("entry_price", 0)
        stop   = conviction.get("stop_code", 0)
        target = conviction.get("target_code", 0)
        result["entrada_rango"]     = f"${entry:.2f}"
        result["stop"]              = f"${stop:.2f}"
        result["target"]            = f"${target:.2f}"
        result["conviction_score"]  = conviction.get("conviction_score")
        result["gate_detail"]       = conviction.get("reasoning", "")
        result["salida_fecha"]      = _calc_exit_date(result.get("horizonte_tiempo", "3-5 dias"))
        result["salida_anticipada"] = (
            f"Salir si baja de ${stop:.2f} (stop ATR) "
            f"o RSI > 78 sin confirmación de volumen"
        )
    else:
        result.setdefault("entrada_rango",    f"${price_data.get('current_price', 0):.2f}")
        result.setdefault("stop",             "N/A")
        result.setdefault("target",           "N/A")
        result.setdefault("salida_fecha",     _calc_exit_date(result.get("horizonte_tiempo", "3-5 dias")))
        result.setdefault("salida_anticipada", "Ver análisis")

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


def _error_result(article: dict, ticker: str, error_msg: str) -> dict:
    return {
        "ticker":              ticker,
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
        "confianza":           "BAJA",
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
