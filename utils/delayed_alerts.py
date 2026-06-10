"""
Delayed Alerts v2.1 — sistema de dos alertas para eventos extraordinarios.

SMS 1: inmediato — aviso de evento, sin señal de entrada
SMS 2: N minutos después — re-evaluación con precio fresco + señal de entrada o descarte

Un solo worker thread procesa la cola. Los items se deduplan por ticker
para evitar colas duplicadas del mismo evento.
"""
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))

FOLLOWUP_DELAY_SECONDS = 7 * 60   # 7 minutos por defecto

# Cola compartida entre threads
_queue: queue.Queue = queue.Queue()
_queued_tickers: set = set()       # dedup: evitar 2 followups del mismo ticker
_queued_lock = threading.Lock()
_worker_started = False


from alerts.twilio_sms import send_raw_message as _send_raw_sms


# ── Worker que procesa la cola ────────────────────────────────────────────────

def _worker():
    """Thread daemon que espera N segundos y luego envía SMS 2."""
    logger.info("[DelayedAlert] Worker iniciado")
    while True:
        try:
            item = _queue.get(timeout=60)
        except queue.Empty:
            continue

        # 2026-06-10: TODO el procesamiento del item va dentro del try — antes el
        # desempaque y el sleep estaban fuera: una excepción ahí mataba el ÚNICO worker
        # para siempre (_worker_started quedaba True y nadie lo reiniciaba).
        ticker = "?"
        try:
            ticker      = item["ticker"]
            fire_at     = item["fire_at"]
            article     = item["article"]
            conviction  = item["conviction"]
            result      = item["result"]
            event_type  = item["event_type"]
            original_price = item["original_price"]
            twilio_to   = item["twilio_to"]

            # Esperar hasta el momento de disparo
            now = time.time()
            if fire_at > now:
                time.sleep(min(fire_at - now, 3600))   # tope sanity: nunca dormir >1h

            _process_followup(
                ticker, article, conviction, result,
                event_type, original_price, twilio_to,
            )
        except Exception as e:
            logger.error(f"[DelayedAlert] Error en followup {ticker}: {e}")
        finally:
            with _queued_lock:
                _queued_tickers.discard(ticker)
            _queue.task_done()


def _process_followup(
    ticker: str,
    article: dict,
    conviction: dict,
    result: dict,
    event_type: str,
    original_price: float,
    twilio_to: str,
):
    """Re-evalúa precio y estabilización, envía SMS 2."""
    from utils.alpaca_price import get_realtime_price
    from utils.event_gate import analyze_stabilization, format_event_confirm_sms

    logger.info(f"[DelayedAlert] Re-evaluando {ticker} — {FOLLOWUP_DELAY_SECONDS//60} min después")

    # Precio fresco
    price_data = get_realtime_price(ticker)

    # Análisis de estabilización con velas actualizadas
    direction = result.get("direccion", "LONG")
    stab = analyze_stabilization(price_data.get("candles_1m", []), direction)

    # Si el precio fresco cambia el entry/stop/target, recalcular
    if price_data.get("current_price") and conviction.get("atr14"):
        from utils.conviction_gates import SECTOR_FOR_ATR, ATR_MULT
        sector = SECTOR_FOR_ATR.get(ticker, "DEFAULT")
        stop_m, target_m = ATR_MULT[sector]
        entry = price_data["current_price"]
        atr   = conviction["atr14"]
        if direction == "LONG":
            result["entrada_rango"] = f"${entry:.2f}"
            result["stop"]   = f"${entry - stop_m * atr:.2f}"
            result["target"] = f"${entry + target_m * atr:.2f}"
        else:
            result["entrada_rango"] = f"${entry:.2f}"
            result["stop"]   = f"${entry + stop_m * atr:.2f}"
            result["target"] = f"${entry - target_m * atr:.2f}"

    body = format_event_confirm_sms(
        ticker, event_type, result, price_data, stab, original_price,
    )

    sent = _send_raw_sms(body, twilio_to)
    logger.info(
        f"[DelayedAlert] SMS 2 {ticker} | signal={stab['signal']} "
        f"| confidence={stab['confidence']} | sent={sent}"
    )


# ── API pública ───────────────────────────────────────────────────────────────

def start_worker():
    """Inicia el worker thread. Llamar una sola vez en main()."""
    global _worker_started
    if _worker_started:
        return
    t = threading.Thread(target=_worker, name="DelayedAlerts", daemon=True)
    t.start()
    _worker_started = True
    logger.info("[DelayedAlert] Worker thread iniciado")


def queue_followup(
    ticker: str,
    article: dict,
    conviction: dict,
    result: dict,
    event_type: str,
    twilio_to: str,
    delay_seconds: int = FOLLOWUP_DELAY_SECONDS,
) -> bool:
    """
    Encola un SMS 2 para N segundos después.
    Retorna False si ya hay un followup pendiente para este ticker (dedup).
    """
    with _queued_lock:
        if ticker in _queued_tickers:
            logger.debug(f"[DelayedAlert] Ya hay followup pendiente para {ticker} — skip")
            return False
        _queued_tickers.add(ticker)

    item = {
        "ticker":         ticker,
        "fire_at":        time.time() + delay_seconds,
        "article":        article,
        "conviction":     conviction,
        "result":         dict(result),   # copia para evitar mutación
        "event_type":     event_type,
        "original_price": result.get("precio_al_alerta") or conviction.get("entry_price", 0),
        "twilio_to":      twilio_to,
    }
    _queue.put(item)
    logger.info(
        f"[DelayedAlert] SMS 2 encolado para {ticker} — "
        f"disparo en {delay_seconds//60} min ({event_type})"
    )
    return True
