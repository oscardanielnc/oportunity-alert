"""
Envío de alertas via Twilio.
Canales soportados: SMS y WhatsApp (configurable en config.json → notification_channel).
WhatsApp es más confiable para números internacionales (ej: Peru).
"""
import os
import logging

logging.getLogger("twilio").setLevel(logging.WARNING)
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Número sandbox de WhatsApp de Twilio (siempre este para cuentas trial/sandbox)
WHATSAPP_SANDBOX_FROM = "whatsapp:+14155238886"


def _get_twilio_client():
    try:
        from twilio.rest import Client
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not sid or not token:
            raise EnvironmentError("TWILIO_ACCOUNT_SID y TWILIO_AUTH_TOKEN deben estar en el entorno")
        return Client(sid, token)
    except ImportError:
        logger.error("twilio no instalado. Ejecutar: pip install twilio")
        return None


def format_sms(result: dict) -> str:
    """Formatea el mensaje de alerta para WhatsApp con formato enriquecido."""
    prioridad = result.get("prioridad", "?")
    ticker = result.get("ticker", "?")
    direccion = result.get("direccion", "?")
    catalizador = result.get("resumen_cataliz", "")
    precio = result.get("precio_al_alerta")
    pct_estimado = result.get("pct_estimado")
    entrada = result.get("entrada_rango", "N/A")
    stop = result.get("stop", "N/A")
    target = result.get("target", "N/A")
    timing = result.get("timing_entrada", "N/A")
    horizonte = result.get("horizonte_tiempo", "N/A")
    riesgo = result.get("riesgo", "")
    salida_anticipada = result.get("salida_anticipada", "")
    source = result.get("source", "")
    age = result.get("age_minutes", 0)

    header_emoji = "🚨" if prioridad == "ALTA" else "⚠️"
    dir_emoji = "📈" if direccion == "LONG" else "📉"
    precio_str = f"${precio:.2f}" if precio else "N/A"
    pct_str = f" (~+{pct_estimado:.0f}% potencial)" if pct_estimado else ""
    fuente_str = "SEC 8-K" if "EDGAR" in source else source.replace("FINNHUB_", "").replace("REDDIT_", "Reddit/")

    # Resumen de catalizador: primera oración o 120 chars
    resumen = catalizador[:120].rsplit(" ", 1)[0] if len(catalizador) > 120 else catalizador

    # Consideraciones antes de entrar (máx 2 líneas)
    consideraciones = []
    if riesgo and riesgo.lower() not in ("n/a", ""):
        consideraciones.append(f"⚠️ {riesgo[:90]}")
    if salida_anticipada and salida_anticipada.lower() not in ("n/a", ""):
        consideraciones.append(f"🚪 {salida_anticipada[:90]}")
    consideraciones_str = "\n".join(consideraciones)

    msg = (
        f"{header_emoji} *{prioridad}*\n"
        f"\n"
        f"*{ticker}* [{direccion}] {dir_emoji}\n"
        f"\n"
        f"{resumen}\n"
        f"_{fuente_str} — hace {age:.0f} min_\n"
        f"\n"
        f"💰 Precio: {precio_str}{pct_str}\n"
        f"📥 Entrada: {entrada}\n"
        f"⏱ Horizonte: {horizonte}\n"
        f"🕐 Timing: {timing}\n"
        f"🛑 Stop: {stop}\n"
        f"🎯 Target: {target}\n"
    )

    if consideraciones_str:
        msg += f"\n{consideraciones_str}\n"

    return msg


def send_sms(result: dict, from_number: str, to_number: str) -> bool:
    """
    Envía alerta via el canal configurado: WhatsApp (preferido) o SMS.
    Canal seleccionado por variable de entorno NOTIFICATION_CHANNEL:
      'whatsapp' → WhatsApp sandbox de Twilio (recomendado para Peru)
      'sms'      → SMS directo (puede fallar con carriers internacionales)
      'both'     → intenta WhatsApp primero, SMS como fallback
    Por defecto usa WhatsApp si no está configurado.
    """
    channel = os.environ.get("NOTIFICATION_CHANNEL", "whatsapp").lower()

    if channel == "whatsapp":
        return _send_whatsapp(result, to_number)
    elif channel == "sms":
        return _send_sms_direct(result, from_number, to_number)
    elif channel == "both":
        sent = _send_whatsapp(result, to_number)
        if not sent:
            sent = _send_sms_direct(result, from_number, to_number)
        return sent
    else:
        logger.warning(f"NOTIFICATION_CHANNEL desconocido: {channel}. Usando whatsapp.")
        return _send_whatsapp(result, to_number)


def _send_whatsapp(result: dict, to_number: str) -> bool:
    """Envía via WhatsApp sandbox de Twilio."""
    client = _get_twilio_client()
    if not client:
        return False

    # Formato WhatsApp: whatsapp:+51929178606
    to_wa = f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number

    try:
        msg_body = format_sms(result)
        message = client.messages.create(
            body=msg_body,
            from_=WHATSAPP_SANDBOX_FROM,
            to=to_wa,
        )
        logger.info(
            f"WhatsApp enviado: {result.get('ticker')} {result.get('prioridad')} "
            f"— SID: {message.sid}"
        )
        return True
    except Exception as e:
        logger.error(f"Error enviando WhatsApp: {e}")
        return False


def _send_sms_direct(result: dict, from_number: str, to_number: str) -> bool:
    """Envía via SMS directo."""
    client = _get_twilio_client()
    if not client:
        return False

    try:
        msg_body = format_sms(result)
        message = client.messages.create(
            body=msg_body,
            from_=from_number,
            to=to_number,
        )
        logger.info(
            f"SMS enviado: {result.get('ticker')} {result.get('prioridad')} "
            f"— SID: {message.sid}"
        )
        return True
    except Exception as e:
        logger.error(f"Error enviando SMS: {e}")
        return False
