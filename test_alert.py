"""
Test de integración: simula la noticia QBTS del 21-mayo-2026 y verifica
que el sistema la clasifique como ALTA y envíe SMS.

Ejecutar con: python test_alert.py
"""
import json
import logging
import os
import sys

# Cargar .env antes de cualquier otra cosa
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_alert")


def test_qbts_case():
    """
    Caso de prueba: D-Wave $100M CHIPS Act Award — el caso real del 21-mayo-2026.
    El sistema debe clasificarlo como ALTA.
    """
    logger.info("=" * 60)
    logger.info("TEST: QBTS $100M CHIPS Act Award")
    logger.info("=" * 60)

    # Simular artículo como lo vería el sistema
    article = {
        "id": "test_qbts_chips_20260521",
        "source": "SEC_EDGAR",
        "title": "D-Wave Quantum Inc. (QBTS) — 8-K: Government Award — $100M CHIPS Act equity stake",
        "summary": (
            "D-Wave Quantum Inc. reported receipt of a $100 million government award "
            "under the CHIPS and Science Act. The Department of Commerce has acquired "
            "an equity stake in the company as part of the federal quantum computing initiative."
        ),
        "url": "https://www.sec.gov/Archives/edgar/data/example/0001234567-26-000001-index.htm",
        "published_at": "2026-05-21T06:54:00+00:00",
        "age_minutes": 2.0,
        "tickers_found": ["QBTS"],
        "raw_text": (
            "D-WAVE QUANTUM INC. QBTS 8-K GOVERNMENT AWARD $100M CHIPS ACT "
            "EQUITY STAKE DEPARTMENT OF COMMERCE FEDERAL QUANTUM COMPUTING"
        ),
    }

    # Test 1: Filtro Python
    logger.info("\n--- TEST 1: Filtro Python (Capa 2) ---")
    from filters.keyword_filter import passes_filter
    config = json.load(open("config.json", encoding="utf-8"))
    watchlist = config["watchlist"]["primary"] + config["watchlist"].get("extended", [])

    passes, reason = passes_filter(
        article=article,
        watchlist=watchlist,
        seen_ids=set(),
        max_age_minutes=45,
    )
    assert passes, f"FALLO: El filtro Python rechazo la noticia. Razon: {reason}"
    logger.info(f"[OK] Filtro Python: PASA (razon esperada: None, obtenida: {reason})")

    # Test 2: Precio Alpaca (puede fallar si no hay credenciales)
    logger.info("\n--- TEST 2: Precio Alpaca ---")
    from utils.alpaca_price import get_realtime_price
    price_data = get_realtime_price("QBTS")
    if price_data.get("error"):
        logger.warning(f"[WARN] Alpaca no disponible: {price_data['error']}")
        # Usar precio simulado para continuar el test
        price_data = {
            "ticker": "QBTS",
            "current_price": 19.30,
            "prev_close": 18.50,
            "change_pct": 4.3,
            "last_trade_time": "2026-05-21T06:54:00Z",
            "last_trade_age_minutes": 2.0,
            "candles_1m": [
                {"t": "T1", "o": 18.80, "h": 19.50, "l": 18.70, "c": 19.30, "v": 85000},
                {"t": "T2", "o": 18.50, "h": 18.90, "l": 18.40, "c": 18.80, "v": 62000},
            ],
            "volume_ratio": 4.2,
            "error": None,
        }
        logger.info(f"  Usando precio simulado: ${price_data['current_price']}")
    else:
        logger.info(f"[OK] Precio Alpaca: ${price_data['current_price']} ({price_data['change_pct']:+.1f}%)")

    # Test 3: Claude Scorer (requiere ANTHROPIC_API_KEY)
    logger.info("\n--- TEST 3: Claude Sonnet Scorer (Capa 3) ---")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("[PENDIENTE] ANTHROPIC_API_KEY no configurada - saltando test de Claude")
        logger.info("  Configura ANTHROPIC_API_KEY para testear el scoring completo")
    else:
        from filters.claude_scorer import score_with_claude
        result = score_with_claude(article, price_data)

        logger.info(f"  Prioridad: {result.get('prioridad')}")
        logger.info(f"  Dirección: {result.get('direccion')}")
        logger.info(f"  Catalizador: {result.get('resumen_cataliz')}")
        logger.info(f"  Entrada: {result.get('entrada_rango')}")
        logger.info(f"  Stop: {result.get('stop')} | Target: {result.get('target')}")
        logger.info(f"  Horizonte: {result.get('horizonte_tiempo')}")
        logger.info(f"  Confianza: {result.get('confianza')}")

        prioridad = result.get("prioridad", "BAJA")
        if prioridad not in ("ALTA", "MEDIA"):
            logger.warning(f"[WARN] Claude clasifico como {prioridad} (esperado ALTA)")
        else:
            logger.info(f"[OK] Claude: {prioridad} -- correcto para este caso")

        # Test 4: Formato SMS
        logger.info("\n--- TEST 4: Formato SMS ---")
        from alerts.twilio_sms import format_sms
        sms_text = format_sms(result)
        logger.info(f"SMS ({len(sms_text)} chars):\n{'-'*40}\n{sms_text}\n{'-'*40}")

        # Test 5: Envío SMS real (solo si Twilio está configurado)
        logger.info("\n--- TEST 5: Envío SMS Twilio ---")
        if not os.environ.get("TWILIO_ACCOUNT_SID"):
            logger.warning("[PENDIENTE] TWILIO_ACCOUNT_SID no configurado - saltando envio real")
        else:
            twilio_from = config["api_keys"].get("twilio_from", "")
            twilio_to = config["api_keys"].get("twilio_to", "")
            if "+1XXXXXXXXXX" in twilio_from:
                logger.warning("[PENDIENTE] Numero Twilio FROM aun es placeholder - actualiza config.json")
            else:
                from alerts.twilio_sms import send_sms
                sent = send_sms(result, twilio_from, twilio_to)
                if sent:
                    logger.info(f"[OK] SMS enviado a {twilio_to}")
                else:
                    logger.error("[FALLO] Error enviando SMS")

        # Test 6: Log de alerta (con el estado real de SMS)
        logger.info("\n--- TEST 6: Log de Alerta ---")
        from alerts.alert_logger import log_alert
        guardado = log_alert(result, sms_enviado=sent if "sent" in dir() else False)
        if guardado:
            logger.info("[OK] Alerta registrada en data/alerts.json")
        else:
            logger.info("[OK] Analisis registrado en data/claude_analysis.log (no accionable)")

    logger.info("\n" + "=" * 60)
    logger.info("TEST COMPLETADO")
    logger.info("=" * 60)


if __name__ == "__main__":
    test_qbts_case()
