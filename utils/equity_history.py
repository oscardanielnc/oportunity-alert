"""
utils/equity_history.py — Historial diario del equity REAL de eToro.

El PositionTracker ya consulta eToro cada 10 min (cash + valor de posiciones).
Aquí persistimos UN snapshot por día (el último gana) del equity total real, para
construir la curva de P&L histórica con plata real en el dashboard.

Diseño:
  - data/equity_history.json: lista [{date, equity, cash, invested, ts}] ordenada por fecha.
  - record_equity_snapshot() es idempotente por día: actualiza la entrada de hoy en vez
    de crear duplicados. Barato (un append/replace de un JSON chico).
  - No llama a eToro: recibe los valores que el tracker ya tiene → cero llamadas extra.
  - Escritura atómica (tmp + replace) para lectores concurrentes (la API).
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))
_DATA_DIR = Path(__file__).parent.parent / "data"
HISTORY_PATH = _DATA_DIR / "equity_history.json"

MAX_DAYS = 730  # ~2 años de historial; recorta lo más viejo


def _read() -> list:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[EquityHistory] archivo ilegible: {e}")
        return []


def _atomic_write(payload: list) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def record_equity_snapshot(equity: float, cash: float, invested: float = None) -> None:
    """
    Registra (o actualiza) el snapshot de equity de HOY (fecha Lima).
    Idempotente: una sola fila por día, el último valor del día gana.
    No hace nada si equity no es un número positivo (eToro caído → no contaminar la curva).
    """
    try:
        equity = float(equity)
    except (TypeError, ValueError):
        return
    if equity <= 0:
        return  # eToro sin respuesta o cuenta vacía → no registrar (mantiene la curva limpia)

    if invested is None:
        invested = round(equity - (cash or 0), 2)

    today = datetime.now(LIMA).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    history = _read()
    row = {
        "date": today,
        "equity": round(equity, 2),
        "cash": round(cash or 0, 2),
        "invested": round(invested, 2),
        "ts": now_iso,
    }
    if history and history[-1].get("date") == today:
        history[-1] = row            # actualizar el snapshot de hoy
    else:
        history.append(row)          # nuevo día

    if len(history) > MAX_DAYS:
        history = history[-MAX_DAYS:]

    _atomic_write(history)


def get_equity_history(days: int = 365) -> dict:
    """
    Retorna la curva de equity para el dashboard.
    {
      "history": [{date, equity, cash, invested}, ...],   # recortado a `days`
      "first_equity", "last_equity", "total_return_pct", "days_tracked"
    }
    """
    history = _read()
    if days and len(history) > days:
        history = history[-days:]
    if not history:
        return {"history": [], "first_equity": None, "last_equity": None,
                "total_return_pct": None, "days_tracked": 0}

    first = history[0]["equity"]
    last = history[-1]["equity"]
    ret = round((last / first - 1) * 100, 2) if first else None
    return {
        "history": history,
        "first_equity": first,
        "last_equity": last,
        "total_return_pct": ret,
        "days_tracked": len(history),
    }
