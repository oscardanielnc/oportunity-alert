"""
utils/market_sentinel.py — Centinela macro por PRECIO (no por titulares).

Nació de la auditoría del crash 2026-06-10: el día que el Dow cayó 500 puntos el
sistema mandó 0 SMS porque todo el pipeline era per-ticker y alcista. Este módulo
mira el MERCADO directo:

  1. QQQ/SPY intradía       (caída fuerte hoy)
  2. QQQ ret_5d             (velocidad de la caída — el gate SMA200 es lentísimo)
  3. Breadth del universo   (% del top-80 de Marea sobre su EMA20)
  4. Posiciones cerca de su chandelier (a <N% del stop)

FASE 1 (esto): SOLO INFORMA — 1 SMS/día máximo cuando hay señal de riesgo, más un
SMS cuando el RÉGIMEN macro (QQQ vs SMA200 ±3% histéresis) cambia de estado.
NO cambia ninguna regla de trading. La fase 2 (apretar el chandelier mientras la
alerta esté activa) se decide con research/backtest_marea_killswitch.py.

Costo: barras diarias 1 fetch/día (QQQ + universo batched); precios en vivo solo
QQQ/SPY por ciclo + held tickers únicamente cuando ya hay trigger.
"""
from __future__ import annotations  # la VM corre Python 3.9 (PEP 604)

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR  = Path(__file__).parent.parent / "data"
STATE_PATH = _DATA_DIR / "market_sentinel_state.json"

# Umbrales por defecto — overrideables desde config.json["macro_sentinel"]
# ret5d/breadth alineados a la zona VALIDADA por research/backtest_marea_killswitch.py
# (2026-06-10): el edge del kill-switch vive en la VELOCIDAD — el disparador −3%/35
# gana (Sharpe 1.69, MaxDD −14.2%); el lento −4%/30 rinde PEOR que no hacer nada.
DEFAULTS = {
    "qqq_intraday_pct":   -2.0,   # QQQ cae esto intradía → trigger
    "spy_intraday_pct":   -1.5,   # SPY (más lento que QQQ) → trigger
    "qqq_ret5d_pct":      -3.0,   # velocidad: retorno 5 días hábiles (zona validada)
    "breadth_min_pct":    35.0,   # % del universo sobre EMA20 por debajo de esto → trigger
    "stop_proximity_pct":  5.0,   # posición a <5% de su chandelier = "en riesgo"
}

# Cache por día: {clave: (fecha_utc, valor)} — breadth y barras QQQ se calculan 1 vez/día
_daily_cache: dict = {}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _cached(key: str):
    item = _daily_cache.get(key)
    if item and item[0] == _today():
        return item[1]
    return None


def _store(key: str, value):
    _daily_cache[key] = (_today(), value)
    return value


def _qqq_bars() -> list:
    """Barras diarias de QQQ (520d), cacheadas por día."""
    cached = _cached("qqq_bars")
    if cached is not None:
        return cached
    try:
        from pilot.momentum_signals import fetch_daily_batch
        bars = fetch_daily_batch(["QQQ"]).get("QQQ", [])
    except Exception as e:
        logger.warning(f"[Sentinel] barras QQQ: {e}")
        bars = []
    if bars:
        _store("qqq_bars", bars)
    return bars


def universe_breadth() -> "float | None":
    """% del universo Marea (top-80) con cierre > EMA20. Cacheado por día.
    Es una medida de cierre diario — suficiente como semáforo (la señal intradía
    la dan QQQ/SPY en vivo)."""
    cached = _cached("breadth")
    if cached is not None:
        return cached
    try:
        from pilot.universe import load_universe
        from pilot.momentum_signals import fetch_daily_batch, _ema
        universe = load_universe()
        if not universe:
            return None
        bars_by_tk = fetch_daily_batch(universe, lookback_days=60)
        above = total = 0
        for tk, bars in bars_by_tk.items():
            closes = [b["c"] for b in bars]
            ema20 = _ema(closes, 20)
            if ema20 is None:
                continue
            total += 1
            if closes[-1] > ema20:
                above += 1
        if total < 20:   # datos truncados → no opinar
            return None
        breadth = round(above / total * 100, 1)
        return _store("breadth", breadth)
    except Exception as e:
        logger.warning(f"[Sentinel] breadth: {e}")
        return None


def _live_change(ticker: str) -> "tuple[float | None, float | None]":
    """(change_pct_hoy, precio_actual) vía get_realtime_price (eToro→Alpaca)."""
    try:
        from utils.alpaca_price import get_realtime_price
        pd = get_realtime_price(ticker)
        return pd.get("change_pct"), pd.get("current_price")
    except Exception as e:
        logger.debug(f"[Sentinel] precio {ticker}: {e}")
        return None, None


def qqq_ret5d(live_price: "float | None" = None) -> "float | None":
    """Retorno % de QQQ en 5 días hábiles, usando el precio vivo si está disponible."""
    bars = _qqq_bars()
    if len(bars) < 6:
        return None
    ref = bars[-6]["c"]
    px = live_price or bars[-1]["c"]
    if not ref:
        return None
    return round((px / ref - 1) * 100, 1)


def check_market_risk(held: "list[dict] | None" = None, thresholds: "dict | None" = None) -> "dict | None":
    """
    Evalúa las señales macro. Retorna None si no hay trigger, o:
      {"triggers": [str], "at_risk": [str], "severity": int}
    `held`: [{"ticker","current","stop"}] — posiciones con su chandelier (opcional).
    """
    th = dict(DEFAULTS)
    th.update(thresholds or {})

    triggers = []

    qqq_chg, qqq_px = _live_change("QQQ")
    if qqq_chg is not None and qqq_chg <= th["qqq_intraday_pct"]:
        triggers.append(f"QQQ {qqq_chg:+.1f}% hoy")

    spy_chg, _ = _live_change("SPY")
    if spy_chg is not None and spy_chg <= th["spy_intraday_pct"]:
        triggers.append(f"SPY {spy_chg:+.1f}% hoy")

    r5 = qqq_ret5d(qqq_px)
    if r5 is not None and r5 <= th["qqq_ret5d_pct"]:
        triggers.append(f"QQQ {r5:+.1f}% en 5 días")

    breadth = universe_breadth()
    if breadth is not None and breadth < th["breadth_min_pct"]:
        triggers.append(f"solo {breadth:.0f}% del universo sobre su EMA20")

    if not triggers:
        return None

    # Posiciones cerca de su chandelier (solo si ya hay trigger — enriquece el SMS)
    at_risk = []
    for p in (held or []):
        cur, stop = p.get("current"), p.get("stop")
        if cur and stop and cur > 0:
            dist = (cur - stop) / cur * 100
            if dist < th["stop_proximity_pct"]:
                at_risk.append(f"{p['ticker']} (a {max(dist, 0):.1f}% del stop)")

    return {
        "triggers": triggers,
        "at_risk": at_risk,
        "severity": len(triggers) + (1 if at_risk else 0),
        "breadth": breadth,
        "qqq_ret5d": r5,
    }


# ── Cambio de régimen (QQQ vs SMA200 con histéresis ±3%) ─────────────────────────

def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        _DATA_DIR.mkdir(exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:
        logger.warning(f"[Sentinel] no pude persistir estado: {e}")


def regime_change() -> "tuple[bool, bool] | None":
    """
    Recalcula el régimen macro (la MISMA función que usa Marea: macro_ok con
    histéresis ±3%) y lo compara con el último estado persistido.
    Retorna (estado_anterior, estado_nuevo) si CAMBIÓ, None si no.
    El primer arranque solo persiste (no avisa).
    """
    bars = _qqq_bars()
    if len(bars) < 201:
        return None
    try:
        from pilot.momentum_signals import macro_ok
        current = bool(macro_ok(bars))
    except Exception as e:
        logger.warning(f"[Sentinel] macro_ok: {e}")
        return None

    state = _load_state()
    prev = state.get("risk_on")
    state["risk_on"] = current
    state["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_state(state)

    if prev is None or prev == current:
        return None
    logger.info(f"[Sentinel] CAMBIO DE RÉGIMEN: {'risk-on' if prev else 'risk-off'} → "
                f"{'risk-on' if current else 'risk-off'}")
    return (prev, current)
