"""
Registro de alertas.
- data/alerts.json     → SOLO alertas accionables (ALTA/MEDIA/BAJA con entrada+stop+target)
- opportunity_alert.log → TODO lo que Claude analizó (incluyendo DESCARTADO)
"""
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

ALERTS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "alerts.json")
ANALYSIS_LOG = os.path.join(os.path.dirname(__file__), "..", "data", "claude_analysis.log")


def _is_actionable(result: dict) -> bool:
    """
    Una alerta es accionable si score_ia >= 1 y tiene entrada, stop y target reales.
    Errores (score_ia=0) y alertas sin operativa no se guardan en historial.
    """
    if int(result.get("score_ia") or 0) < 1:
        return False
    entrada = result.get("entrada_rango", "N/A") or "N/A"
    stop = result.get("stop", "N/A") or "N/A"
    target = result.get("target", "N/A") or "N/A"
    return (
        entrada.upper() not in ("N/A", "", "NONE")
        and stop.upper() not in ("N/A", "", "NONE")
        and target.upper() not in ("N/A", "", "NONE")
    )


def log_claude_analysis(result: dict) -> None:
    """
    Registra en texto legible TODO lo que Claude analizó,
    incluyendo DESCARTADO. Solo va al archivo de análisis, no al historial.
    """
    analysis_path = os.path.abspath(ANALYSIS_LOG)
    os.makedirs(os.path.dirname(analysis_path), exist_ok=True)

    ticker = result.get("ticker", "?")
    score_ia = int(result.get("score_ia") or 0)
    prioridad = result.get("prioridad", "?")
    direccion = result.get("direccion", "?")
    catalizador = result.get("resumen_cataliz", "?")
    precio = result.get("precio_al_alerta")
    source = result.get("source", "?")
    age = result.get("age_minutes", 0)
    engine = result.get("ai_engine", "?")
    error = result.get("error", "")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if error:
        line = (
            f"[{now}] ERROR | {ticker} | {source} | "
            f"hace {age:.0f}min | engine={engine} | {error}"
        )
    else:
        precio_str = f"${precio:.2f}" if precio else "N/A"
        entrada = result.get("entrada_rango", "N/A")
        stop = result.get("stop", "N/A")
        target = result.get("target", "N/A")
        timing = result.get("timing_entrada", "N/A")
        horizonte = result.get("horizonte_tiempo", "N/A")
        pct = result.get("pct_estimado", 0)
        pct_str = f" ~+{pct:.0f}%" if pct else ""

        line = (
            f"[{now}] {score_ia}/10 {prioridad:<5} | {ticker:<6} {direccion:<5} | "
            f"precio={precio_str}{pct_str} | fuente={source} | hace {age:.0f}min\n"
            f"             catalizador: {catalizador}\n"
            f"             operativa: entrada={entrada} | stop={stop} | target={target}\n"
            f"             timing={timing} | horizonte={horizonte}"
        )

    try:
        with open(analysis_path, "a", encoding="utf-8") as f:
            f.write(line + "\n" + "-" * 80 + "\n")
    except Exception as e:
        logger.error(f"Error escribiendo analysis log: {e}")

    # También al logger principal (una línea resumida)
    if error:
        logger.error(f"[CLAUDE ERROR] {ticker}: {error}")
    else:
        stars = "★" * min(score_ia, 10) + "☆" * (10 - min(score_ia, 10))
        logger.info(
            f"[CLAUDE {score_ia}/10] {ticker} {prioridad} {direccion} | "
            f"precio={precio_str}{pct_str} | {catalizador[:70]}"
        )


def log_alert(result: dict, sms_enviado: bool = False, _skip_analysis_log: bool = False) -> bool:
    """
    Guarda en data/alerts.json SOLO si es accionable (tiene entrada+stop+target).
    Por defecto también registra en claude_analysis.log; pasar _skip_analysis_log=True
    si ya se llamó a log_claude_analysis antes para evitar duplicados.
    Retorna True si fue guardado en historial.
    """
    if not _skip_analysis_log:
        log_claude_analysis(result)

    # Solo guardar en historial si es accionable
    if not _is_actionable(result):
        prioridad = result.get("prioridad", "?")
        ticker = result.get("ticker", "?")
        logger.info(
            f"[HISTORIAL] {ticker} {prioridad} — no accionable (sin entrada/stop/target), "
            f"solo en claude_analysis.log"
        )
        return False

    alerts_path = os.path.abspath(ALERTS_FILE)
    os.makedirs(os.path.dirname(alerts_path), exist_ok=True)

    result["sms_enviado"] = sms_enviado

    try:
        with open(alerts_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
        logger.info(
            f"[HISTORIAL] {result.get('ticker')} {result.get('prioridad')} guardado "
            f"— SMS: {'SI' if sms_enviado else 'NO'}"
        )
        return True
    except Exception as e:
        logger.error(f"Error escribiendo historial: {e}")
        return False


def update_24h_prices(price_fetcher_fn) -> int:
    """Actualiza precio_24h_despues en alertas del día anterior."""
    alerts_path = os.path.abspath(ALERTS_FILE)
    if not os.path.exists(alerts_path):
        return 0

    updated = 0
    now_utc = datetime.now(timezone.utc)
    lines_out = []

    try:
        with open(alerts_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                alert = json.loads(line)
                ts_str = alert.get("timestamp", "")
                if ts_str and alert.get("precio_24h_despues") is None:
                    ts = dateparser.parse(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_hours = (now_utc - ts).total_seconds() / 3600
                    if 23 <= age_hours <= 25:
                        ticker = alert.get("ticker", "")
                        if ticker:
                            price_data = price_fetcher_fn(ticker)
                            if price_data.get("current_price"):
                                alert["precio_24h_despues"] = price_data["current_price"]
                                updated += 1
                lines_out.append(json.dumps(alert, ensure_ascii=False))
            except Exception as e:
                logger.warning(f"Error en update_24h_prices línea: {e}")
                lines_out.append(line)

        if updated > 0:
            with open(alerts_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines_out) + "\n")
            logger.info(f"Precios 24h actualizados: {updated} alertas")

    except Exception as e:
        logger.error(f"Error en update_24h_prices: {e}")

    return updated


def get_recent_alerts(hours: int = 24) -> list[dict]:
    """Retorna alertas accionables de las últimas N horas."""
    alerts_path = os.path.abspath(ALERTS_FILE)
    if not os.path.exists(alerts_path):
        return []

    alerts = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    try:
        with open(alerts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    alert = json.loads(line)
                    ts_str = alert.get("timestamp", "")
                    if ts_str:
                        ts = dateparser.parse(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            alerts.append(alert)
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")

    return alerts
