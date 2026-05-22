"""
Capa 3: Scoring con IA.
Motor primario: Gemini 2.0 Flash (gratis hasta 1,500 req/día).
Motor secundario: Claude Haiku/Sonnet (si se configura ANTHROPIC_API_KEY).
Selección via config.json → campo "ai_engine": "gemini" | "claude"
"""
import json
import logging
import os
from datetime import datetime, timezone

from utils.alpaca_price import summarize_candles

logger = logging.getLogger(__name__)


# ── Prompt compartido ─────────────────────────────────────────────────────────

def _build_prompt(article: dict, price_data: dict) -> tuple[str, str]:
    """Retorna (system_prompt, user_prompt)."""
    ticker = (article.get("tickers_found") or ["UNKNOWN"])[0]
    title = article.get("title", "")
    summary = article.get("summary", "")
    age_minutes = article.get("age_minutes", 0)
    source = article.get("source", "Desconocida")

    current_price = price_data.get("current_price")
    prev_close = price_data.get("prev_close")
    change_pct = price_data.get("change_pct")
    last_trade_time = price_data.get("last_trade_time", "N/A")
    last_trade_age = price_data.get("last_trade_age_minutes")
    candles = price_data.get("candles_1m", [])
    candle_summary = summarize_candles(candles)
    price_error = price_data.get("error")

    now_lima = datetime.now(timezone.utc)
    hora_lima = now_lima.strftime("%H:%M Lima")

    price_warning = ""
    if price_error:
        price_warning = f"AVISO: Error Alpaca: {price_error} - precio puede no estar disponible"
    elif last_trade_age and last_trade_age > 10:
        price_warning = f"AVISO: Precio tiene {last_trade_age:.0f} minutos de antiguedad - mercado cerrado"

    # Clasificar sector para calibrar umbrales de volatilidad
    QUANTUM = {"QBTS", "IONQ", "RGTI", "QUBT"}
    SMALL_AI = {"SOUN", "BBAI", "INOD", "APLD", "IREN"}
    SPACE_EVTOL = {"RKLB", "LUNR", "JOBY", "ACHR"}
    BTC_MINERS = {"MARA", "RIOT", "MSTR"}
    MEME = {"GME", "AMC"}
    if ticker in QUANTUM:
        sector_context = (
            f"{ticker} es computacion cuantica — sector historicamente explosivo ante catalizadores "
            f"gubernamentales (QBTS +33% en mayo 2026, IONQ +27% mismo dia). "
            f"Movimientos de 30-150% en 1-3 dias son normales. Umbral 'ya priceado': 30%."
        )
        priced_threshold = 30
    elif ticker in BTC_MINERS:
        sector_context = (
            f"{ticker} es minero de Bitcoin — se mueve 2-3x el movimiento de BTC. "
            f"Alta volatilidad intrinseca, puede subir 40-80% en un dia con catalizador. "
            f"Umbral 'ya priceado': 25%."
        )
        priced_threshold = 25
    elif ticker in SMALL_AI:
        sector_context = (
            f"{ticker} es AI de pequena capitalizacion — reacciona con fuerza a contratos gobierno "
            f"o partnerships. Movimientos de 30-100% en horas son normales. Umbral 'ya priceado': 25%."
        )
        priced_threshold = 25
    elif ticker in SPACE_EVTOL:
        sector_context = (
            f"{ticker} es espacio/eVTOL — contratos DoD o hitos de produccion mueven 20-60%. "
            f"Umbral 'ya priceado': 20%."
        )
        priced_threshold = 20
    elif ticker in MEME:
        sector_context = (
            f"{ticker} es meme stock — squeezes pueden ir 50-300% en horas. "
            f"El momentum social es el catalizador, no los fundamentales. Umbral 'ya priceado': 30%."
        )
        priced_threshold = 30
    elif ticker in {"NVDA", "TSLA", "SMCI", "RIVN", "LCID", "WOLF", "PLTR", "APP"}:
        sector_context = (
            f"{ticker} es accion de alta volatilidad — puede mover 15-40% ante catalizadores directos. "
            f"Umbral 'ya priceado': 15%."
        )
        priced_threshold = 15
    else:
        sector_context = (
            f"{ticker} es accion de volatilidad moderada-alta. Umbral 'ya priceado': 10%."
        )
        priced_threshold = 10

    # Ajuste para noticias breaking (<5 min): el precio puede seguir corriendo
    breaking_note = ""
    if age_minutes < 5:
        breaking_note = (
            f"NOTA CRITICA: Esta noticia tiene solo {age_minutes:.0f} minutos. "
            f"Es breaking news — el movimiento inicial no significa que el catalizador este agotado. "
            f"Solo descartar si el precio ya supero el {priced_threshold}% de movimiento."
        )

    system_prompt = (
        "Eres el sistema de alertas de trading de Oscar, 25 anos, ingeniero, capital $6,000 en eToro. "
        "Oscar tiene alta tolerancia al riesgo y busca movimientos de 15-100%+ en 1-5 dias. "
        "Opera LONG y SHORT en eToro 24h (pre-market, regular, after-hours) de lunes a viernes 3pm Lima. "
        "Para crypto opera en Binance. "
        "Este sistema es el DETECTOR — Oscar tiene otro sistema de analisis tecnico+fundamental que "
        "confirma cada entrada antes de operar. Tu trabajo: NO FILTRAR EN EXCESO. "
        "Detectar y alertar. Oscar decide. "
        "Posicion tipica: $300-$1,200 (5-20% del capital). "
        "SESGO OBLIGATORIO: ante duda, alertar. Perder una entrada de +40% es mucho peor "
        "que recibir una alerta que no se materializa. "
        "Responde SOLO con JSON valido, sin texto adicional, sin markdown, sin codigo de bloque."
    )

    user_prompt = f"""NOTICIA A EVALUAR:
Ticker: {ticker}
Fuente: {source}
Publicada: hace {age_minutes:.0f} minutos
Hora actual: {hora_lima}
Precio actual REAL: ${current_price if current_price else 'NO DISPONIBLE'} (Alpaca — incluye AH/pre-market)
Precio cierre anterior: ${prev_close if prev_close else 'N/A'}
Cambio desde cierre: {f'{change_pct:+.1f}%' if change_pct is not None else 'N/A'}
Momentum ultimas velas 1min: {candle_summary}
Hora del ultimo trade: {last_trade_time}
{price_warning}

CONTEXTO DEL SECTOR:
{sector_context}
{breaking_note}

Titular: {title}
Resumen: {summary}

EVALUA en este orden exacto:

1. ESTADO DEL CATALIZADOR:
   - El evento ocurrio o esta pendiente?
   - El precio ya supero el umbral de priceado ({priced_threshold}%) para este sector? (SI/NO)
   - Si supero ese umbral Y la noticia tiene >5 min -> DESCARTADO
   - Si la noticia tiene <5 min: evaluar si el momentum indica continuacion o agotamiento

2. DIRECCION: LONG o SHORT + razon en 1 linea

3. MAGNITUD ESTIMADA: % adicional potencial en proximas 4-24 horas (desde precio actual, no desde cierre)

4. PRIORIDAD:
   - ALTA: catalizador directo + empresa nombrada + no priceado o breaking con continuacion + >8% adicional
   - MEDIA: catalizador real + potencial 3-8% adicional OR breaking news con catalizador solido aunque ya movio algo
   - BAJA: senal debil, sector general, o potencial <3% adicional
   - DESCARTADO: catalizador completamente absorbido por el precio (supero umbral del sector) o noticia vieja sin movimiento
   - REGLA CRITICA: duda entre MEDIA y BAJA -> usar MEDIA siempre

5. OPERATIVA (obligatorio si ALTA o MEDIA):
   - Entrada: precio actual o rango realista (Oscar opera en eToro AH/pre-market ahora mismo)
   - Stop: nivel de invalidacion del catalizador
   - Target: precio objetivo basado en movimientos historicos del sector
   - Timing: cuando entrar (ahora en AH / apertura 8:30am Lima / esperar pullback a $X)
   - Horizonte: cuantos dias mantener la posicion
   - Riesgo principal: 1 linea

Responde SOLO con este JSON exacto, sin texto adicional:
{{
  "ticker": "{ticker}",
  "prioridad": "ALTA|MEDIA|BAJA|DESCARTADO",
  "tipo_catalizador": "fundamental|hype|mixto",
  "direccion": "LONG|SHORT",
  "pct_estimado": 0.0,
  "catalizador_priceado": false,
  "resumen_cataliz": "descripcion en 1 linea del catalizador",
  "entrada_rango": "$X.XX - $X.XX",
  "stop": "$X.XX",
  "target": "$X.XX",
  "timing_entrada": "ahora AH en eToro | apertura 8:30am Lima | esperar pullback a $X",
  "horizonte_tiempo": "mismo dia | Day+1 | 3-5 dias",
  "salida_fecha": "YYYY-MM-DD HH:MMam Lima si no llego a target",
  "salida_anticipada": "salir si baja de $X.XX (stop) o momentum no confirma en Xh",
  "riesgo": "descripcion del riesgo principal",
  "confianza": "ALTA|MEDIA|BAJA"
}}"""

    return system_prompt, user_prompt


# ── Motor Gemini ──────────────────────────────────────────────────────────────

def _score_with_gemini(article: dict, price_data: dict, model: str = "gemini-2.0-flash") -> dict:
    """
    Scoring via Google Gemini API (gratis hasta 1,500 req/dia).
    Requiere: GEMINI_API_KEY (gratuita en aistudio.google.com)
    """
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai no instalado. Ejecutar: pip install google-generativeai"
        )

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY no configurada")

    genai.configure(api_key=api_key)
    system_prompt, user_prompt = _build_prompt(article, price_data)

    gemini_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_prompt,
        generation_config={
            "temperature": 0.1,       # baja temperatura = respuestas más consistentes
            "response_mime_type": "application/json",  # fuerza JSON puro
        },
    )

    response = gemini_model.generate_content(user_prompt)
    response_text = response.text.strip()

    # Limpiar markdown si lo incluyó de todos modos
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    return json.loads(response_text)


# ── Motor Claude ──────────────────────────────────────────────────────────────

def _score_with_claude(
    article: dict,
    price_data: dict,
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    """
    Scoring via Anthropic Claude API.
    Requiere: ANTHROPIC_API_KEY (console.anthropic.com)
    Costo estimado con Haiku: ~$2/mes para 30 analisis/dia.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError("anthropic no instalado. Ejecutar: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY no configurada")

    system_prompt, user_prompt = _build_prompt(article, price_data)
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    return json.loads(response_text)


# ── Función principal ─────────────────────────────────────────────────────────

def score_with_claude(article: dict, price_data: dict, model: str = None) -> dict:
    """
    Punto de entrada principal. Elige el motor según config.
    Nombre mantenido como 'score_with_claude' para compatibilidad con main.py.
    """
    # Leer configuración de engine desde variable de entorno o config
    ai_engine = os.environ.get("AI_ENGINE", "gemini").lower()

    ticker = (article.get("tickers_found") or ["UNKNOWN"])[0]

    try:
        if ai_engine == "gemini":
            gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            logger.info(f"Scoring con Gemini {gemini_model} — {ticker}")
            result = _score_with_gemini(article, price_data, model=gemini_model)

        elif ai_engine == "claude":
            claude_model = model or os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
            logger.info(f"Scoring con Claude {claude_model} — {ticker}")
            result = _score_with_claude(article, price_data, model=claude_model)

        else:
            raise ValueError(f"AI_ENGINE desconocido: {ai_engine}. Usar 'gemini' o 'claude'")

        # Agregar metadatos
        result["article_id"] = article.get("id", "")
        result["article_url"] = article.get("url", "")
        result["source"] = article.get("source", "")
        result["age_minutes"] = article.get("age_minutes", 0)
        result["precio_al_alerta"] = price_data.get("current_price")
        result["precio_24h_despues"] = None
        result["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-05:00")
        result["ai_engine"] = ai_engine

        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON invalido del modelo: {e}")
        return _error_result(article, ticker, f"JSON invalido: {e}")
    except (EnvironmentError, ImportError) as e:
        logger.error(str(e))
        return _error_result(article, ticker, str(e))
    except Exception as e:
        logger.error(f"Error en scoring: {e}")
        return _error_result(article, ticker, str(e))


def _error_result(article: dict, ticker: str, error_msg: str) -> dict:
    return {
        "ticker": ticker,
        "prioridad": "ERROR",
        "tipo_catalizador": "desconocido",
        "direccion": "N/A",
        "pct_estimado": 0.0,
        "catalizador_priceado": False,
        "resumen_cataliz": f"Error en scoring: {error_msg}",
        "entrada_rango": "N/A",
        "stop": "N/A",
        "target": "N/A",
        "timing_entrada": "N/A",
        "horizonte_tiempo": "N/A",
        "salida_fecha": "N/A",
        "salida_anticipada": "N/A",
        "riesgo": "Error en analisis",
        "confianza": "BAJA",
        "article_id": article.get("id", ""),
        "article_url": article.get("url", ""),
        "source": article.get("source", ""),
        "age_minutes": article.get("age_minutes", 0),
        "precio_al_alerta": None,
        "precio_24h_despues": None,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-05:00"),
        "ai_engine": os.environ.get("AI_ENGINE", "gemini"),
        "error": error_msg,
    }
