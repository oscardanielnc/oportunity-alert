"""
OportunityAlert — Historial de alertas accionables
Ejecutar con: python show_history.py

Muestra las últimas 24h ordenado por prioridad (ALTA > MEDIA > BAJA)
y dentro de cada grupo por fecha (más reciente primero).
Solo muestra alertas con entrada, stop y target definidos.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ALERTS_FILE = os.path.join(os.path.dirname(__file__), "data", "alerts.json")
ANALYSIS_LOG = os.path.join(os.path.dirname(__file__), "data", "claude_analysis.log")

PRIORITY_ORDER = {"ALTA": 0, "MEDIA": 1, "BAJA": 2}
WIDTH = 72


def _load_alerts(hours: int = 24) -> list[dict]:
    if not os.path.exists(ALERTS_FILE):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    alerts = []
    with open(ALERTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
                ts_str = a.get("timestamp", "")
                if ts_str:
                    ts = dateparser.parse(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        a["_ts"] = ts
                        alerts.append(a)
            except Exception:
                continue
    return alerts


def _sort_alerts(alerts: list[dict]) -> list[dict]:
    """Ordena por prioridad (ALTA>MEDIA>BAJA) y dentro de cada grupo por fecha desc."""
    return sorted(
        alerts,
        key=lambda a: (
            PRIORITY_ORDER.get(a.get("prioridad", "BAJA"), 9),
            -a["_ts"].timestamp(),
        ),
    )


def _format_time(ts: datetime) -> str:
    """Hora en Lima (UTC-5)."""
    lima = ts.astimezone(timezone(timedelta(hours=-5)))
    return lima.strftime("%d/%m %H:%M")


def _pct_resultado(alert: dict) -> str:
    """Calcula resultado real si hay precio 24h."""
    p0 = alert.get("precio_al_alerta")
    p1 = alert.get("precio_24h_despues")
    if p0 and p1 and p0 > 0:
        pct = (p1 - p0) / p0 * 100
        signo = "+" if pct >= 0 else ""
        return f"Resultado 24h: {signo}{pct:.1f}%  (${p0:.2f}→${p1:.2f})"
    return ""


def _bar(prioridad: str) -> str:
    bars = {"ALTA": "█" * 5, "MEDIA": "█" * 3 + "░" * 2, "BAJA": "█" + "░" * 4}
    return bars.get(prioridad, "░" * 5)


def _render_alert(a: dict, index: int) -> str:
    prioridad = a.get("prioridad", "?")
    ticker = a.get("ticker", "?")
    direccion = a.get("direccion", "?")
    catalizador = a.get("resumen_cataliz", "?")
    confianza = a.get("confianza", "?")
    entrada = a.get("entrada_rango", "N/A")
    stop = a.get("stop", "N/A")
    target = a.get("target", "N/A")
    timing = a.get("timing_entrada", "N/A")
    horizonte = a.get("horizonte_tiempo", "N/A")
    salida_fecha = a.get("salida_fecha", "")
    salida_anticipada = a.get("salida_anticipada", "")
    source = a.get("source", "?").replace("SEC_EDGAR", "EDGAR").replace("FINNHUB_", "").replace("REDDIT_", "Reddit/")
    precio = a.get("precio_al_alerta")
    sms = "SI" if a.get("sms_enviado") else "no"
    ts = a.get("_ts")
    hora_str = _format_time(ts) if ts else "?"
    resultado = _pct_resultado(a)
    tipo = a.get("tipo_catalizador", "?")
    engine = a.get("ai_engine", "?")

    precio_str = f"${precio:.2f}" if precio else "N/A"
    dir_icon = "LONG ^" if direccion == "LONG" else "SHORT v" if direccion == "SHORT" else direccion

    lines = []
    lines.append(f"  #{index}  {hora_str} Lima  |  {ticker}  {dir_icon}  |  confianza: {confianza}  {_bar(prioridad)}")
    # Wrap catalizador en líneas de WIDTH-4 chars
    cat_words = catalizador.split()
    cat_line = "  "
    for word in cat_words:
        if len(cat_line) + len(word) + 1 > WIDTH:
            lines.append(cat_line)
            cat_line = "  " + word + " "
        else:
            cat_line += word + " "
    if cat_line.strip():
        lines.append(cat_line)
    lines.append(f"")
    lines.append(f"  Entrada : {entrada}")
    lines.append(f"  Stop    : {stop}   |   Target: {target}")
    lines.append(f"  Timing  : {timing}")
    lines.append(f"  Salir   : {horizonte}")
    if salida_fecha:
        lines.append(f"  Fecha   : {salida_fecha}")
    if salida_anticipada:
        lines.append(f"  Anticip : {salida_anticipada[:WIDTH - 10]}")
    lines.append(f"")
    lines.append(f"  Fuente: {source}  |  Precio alerta: {precio_str}  |  SMS: {sms}  |  Tipo: {tipo}")
    if resultado:
        lines.append(f"  {resultado}")
    return "\n".join(lines)


def _count_analysis() -> int:
    """Cuenta cuántos análisis hizo Claude en las últimas 24h."""
    if not os.path.exists(ANALYSIS_LOG):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    try:
        with open(ANALYSIS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("[20") and "]" in line:
                    try:
                        ts_str = line[1:20]
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            count += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return count


def show(hours: int = 24):
    alerts = _load_alerts(hours)

    if not alerts:
        print("=" * WIDTH)
        print("  OPPORTUNITY ALERT — Sin alertas accionables en las últimas 24h")
        print("=" * WIDTH)
        print(f"  Revisa data/claude_analysis.log para ver todo lo que Claude analizó.")
        return

    sorted_alerts = _sort_alerts(alerts)
    total = len(alerts)
    n_alta = sum(1 for a in alerts if a.get("prioridad") == "ALTA")
    n_media = sum(1 for a in alerts if a.get("prioridad") == "MEDIA")
    n_baja = sum(1 for a in alerts if a.get("prioridad") == "BAJA")
    n_analysis = _count_analysis()

    now_lima = datetime.now(timezone(timedelta(hours=-5)))
    fecha_str = now_lima.strftime("%A %d/%m/%Y  %H:%M Lima")

    print()
    print("=" * WIDTH)
    print(f"  OPPORTUNITY ALERT — Historial ultimas {hours}h")
    print(f"  {fecha_str}")
    print("=" * WIDTH)
    print(
        f"  Claude analizo: {n_analysis} noticias  |  "
        f"Accionables: {total}  "
        f"(ALTA:{n_alta}  MEDIA:{n_media}  BAJA:{n_baja})"
    )
    print("=" * WIDTH)

    current_priority = None
    index = 1

    for a in sorted_alerts:
        prioridad = a.get("prioridad", "BAJA")

        if prioridad != current_priority:
            current_priority = prioridad
            label = {
                "ALTA": "  ALTA  — accion urgente",
                "MEDIA": "  MEDIA — considerar entrada",
                "BAJA": "  BAJA  — seguimiento",
            }.get(prioridad, f"  {prioridad}")
            print()
            print(f"{'─' * WIDTH}")
            print(label)
            print(f"{'─' * WIDTH}")

        print()
        print(_render_alert(a, index))
        print()
        print(f"  {'·' * (WIDTH - 2)}")
        index += 1

    print()
    print("=" * WIDTH)
    print(f"  {total} alertas  |  log completo: data/claude_analysis.log")
    print("=" * WIDTH)
    print()


if __name__ == "__main__":
    # Acepta argumento opcional de horas: python show_history.py 48
    hours = 24
    if len(sys.argv) > 1:
        try:
            hours = int(sys.argv[1])
        except ValueError:
            pass
    show(hours=hours)
