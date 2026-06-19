"""
alerts/callmebot_whatsapp.py — Canal de WhatsApp via CallMeBot (gratis, uso personal).

Alternativa al sandbox de Twilio (que expira ~72h y obliga a re-hacer "join"). CallMeBot
es un servicio de un tercero: un solo GET HTTP envía un WhatsApp a TU propio número.

⚠️ Consideraciones (evaluadas 2026-06-18):
  - Tus mensajes pasan por el servidor de CallMeBot (tercero, un solo dev): no es privado
    y no tiene SLA. Por eso el sistema lo usa con FALLBACK a Twilio (ver twilio_sms.py):
    si CallMeBot falla o no confirma, la alerta NO se pierde.
  - Solo uso personal, a tu propio número. La "apikey" no es un secreto fuerte (va en la URL).

Setup (una vez) — confirma el número vigente en callmebot.com/blog/free-api-whatsapp-messages:
  1. Agenda el número que indique su web (a 2026-06-18 era +34 694 242 562) como contacto.
  2. Envíale por WhatsApp:  "I allow callmebot to send me messages"
  3. Te responde con tu API key.
  4. En el entorno (.env / systemd EnvironmentFile):
        NOTIFICATION_CHANNEL=callmebot
        CALLMEBOT_PHONE=+51XXXXXXXXX      # tu número con código de país
        CALLMEBOT_APIKEY=123456           # la key que te dio el bot

send_message(body) -> bool   # True solo si CallMeBot CONFIRMA encolado/enviado
"""
import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.callmebot.com/whatsapp.php"
_TIMEOUT  = 20            # seg: no colgar el pipeline si CallMeBot tarda
_RETRY_ATTEMPTS = 3
_RETRY_DELAYS   = (3, 6)  # seg entre intentos

# CallMeBot descarta mensajes enviados demasiado rápido seguidos. Espaciamos los envíos
# un mínimo entre sí (con lock global) para no perder alertas en ráfagas.
_MIN_SPACING_S = 4.0
_last_send_at  = 0.0
_send_lock     = threading.Lock()

# Marcadores en la respuesta (texto/HTML 200) que indican éxito vs error.
_OK_MARKERS  = ("queued", "message sent", "will receive", "success")
_ERR_MARKERS = ("error", "invalid", "not valid", "apikey", "you need", "not found",
                "missing", "denied", "didn't accept", "did not accept")


def _config() -> tuple:
    phone  = os.environ.get("CALLMEBOT_PHONE", "").strip()
    apikey = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    return phone, apikey


def is_configured() -> bool:
    phone, apikey = _config()
    return bool(phone and apikey)


def _looks_ok(text: str) -> bool:
    """CallMeBot responde 200 con texto/HTML. Éxito = marcador OK y sin marcador de error."""
    t = (text or "").lower()
    if any(m in t for m in _OK_MARKERS) and not any(m in t for m in _ERR_MARKERS):
        return True
    return False


def _respect_spacing() -> None:
    global _last_send_at
    wait = 0.0
    with _send_lock:
        delta = time.time() - _last_send_at
        if delta < _MIN_SPACING_S:
            wait = _MIN_SPACING_S - delta
        # reservamos el slot ya (incluyendo la espera) para serializar ráfagas concurrentes
        _last_send_at = time.time() + wait
    if wait > 0:
        time.sleep(wait)


def send_message(body: str) -> bool:
    """
    Envía `body` por WhatsApp via CallMeBot a CALLMEBOT_PHONE. Devuelve True SOLO si la
    respuesta confirma encolado/enviado — así el caller puede degradar a Twilio si es False.
    Estricto a propósito: un falso negativo solo provoca un duplicado por el fallback;
    un falso positivo perdería la alerta (peor en trading).
    """
    phone, apikey = _config()
    if not phone or not apikey:
        logger.error("[CallMeBot] CALLMEBOT_PHONE / CALLMEBOT_APIKEY no configurados")
        return False

    params = {"phone": phone, "text": body, "apikey": apikey}
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            _respect_spacing()
            r = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT)
            if r.status_code == 200 and _looks_ok(r.text):
                if attempt > 0:
                    logger.info(f"[CallMeBot] enviado al intento {attempt + 1}")
                logger.info(f"[CallMeBot] WhatsApp enviado ({len(body)} chars)")
                return True
            snippet = (r.text or "")[:160].replace("\n", " ")
            logger.warning(
                f"[CallMeBot] intento {attempt + 1} sin confirmación "
                f"(HTTP {r.status_code}): {snippet}"
            )
        except Exception as e:
            logger.warning(f"[CallMeBot] intento {attempt + 1} error: {e}")
        if attempt < _RETRY_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])

    logger.error(f"[CallMeBot] agotados {_RETRY_ATTEMPTS} intentos — el caller hará fallback")
    return False
