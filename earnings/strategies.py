#!/usr/bin/env python3
"""
Registro de ESTRATEGIAS de earnings — cada una produce CANDIDATOS (no trades).

Un candidato es un dict uniforme que la sección muestra y el Scoreboard mide:
  {strategy, ticker, direction, category, conviction, reaction_pct, price_t0, star, note}

Hoy: PED (Post-Earnings Drift, LONG, validado). Para sumar una estrategia (Short-PED,
Cañonazo, pre-run-up) basta agregar una función generadora y registrarla en STRATEGIES.
La math de señal se reutiliza de pilot.ped_signals (librería neutral).
"""
from pilot.ped_signals import ped_entry_candidates, MEGA_CAP_PED, PED_HOLD_DAYS


def ped_candidates(data, earnings_map, ref_date):
    """PED LONG: mega-cap ≥$100B cuyo Day+1 (=ref_date) reaccionó +5..+25% sin gap-down >5%.
    Convicción = magnitud de la reacción (mayor reacción dentro del rango = drift más fuerte)."""
    out = []
    for tk, rx in ped_entry_candidates(data, earnings_map, ref_date):
        bars = data.get(tk) or []
        close = bars[-1]["c"] if bars else None
        out.append({
            "strategy": "PED", "ticker": tk, "direction": "LONG", "category": "ped",
            "conviction": rx, "reaction_pct": rx, "price_t0": close, "star": rx >= 10.0,
            "note": f"Compra Day+2 open, sale ~Day+{PED_HOLD_DAYS+1} (drift institucional)",
        })
    return out


# ── PRE-RUN-UP (pre-posicionamiento) — estrategia PRE, anota el CALENDARIO ──────────────────────
# Validada en research/backtest_prerun.py (n=589, 40 mega-caps, 4 años, EXCESS vs QQQ, neto fee,
# OOS): entrar ~5 ruedas antes del reporte en nombres SOBRE su SMA50, salir en el último cierre
# ANTES del earning (cero exposición al evento). Edge modesto pero real en mega-caps líquidos.
PRERUN_K_TRADING = 5          # ruedas (días hábiles) antes del reporte = entrada
PRERUN_ENTRY_CAL_DAYS = 7     # ≈ 5 ruedas en días calendario (para fechar la entrada)
PRERUN_SMA = 50               # filtro de tendencia: close > SMA50 en la entrada
# Proyección histórica (K=5, tendencia, raw bruto + excess vs QQQ) — "cuánto puede subir".
PRERUN_PROJ = {
    "raw_median": 1.3, "raw_mean": 1.4, "p25": -2.2, "p75": 5.0,
    "win": 58, "excess_median": 0.9,
    "rule": "Entrar ~5 ruedas antes del reporte si está sobre su SMA50; salir en el último "
            "cierre ANTES del earning (no se aguanta el reporte).",
}


def prerun_for(ticker, days_until, bars):
    """Aplicabilidad del pre-run-up a un earning PRÓXIMO. Retorna dict o None (sin barras).
    applies = nombre sobre su SMA50 (filtro de tendencia validado)."""
    closes = [b["c"] for b in (bars or []) if b.get("c")]
    if len(closes) < PRERUN_SMA + 1:
        return None
    price = closes[-1]
    sma = sum(closes[-PRERUN_SMA:]) / PRERUN_SMA
    above = price > sma
    return {"applies": bool(above), "above_sma": bool(above),
            "k_trading": PRERUN_K_TRADING,
            "entry_in_days": days_until - PRERUN_ENTRY_CAL_DAYS}


# Cada entrada: name (etiqueta), universe (tickers a vigilar), needs ('recent_earnings' = la
# estrategia consume el mapa de earnings YA reportados), fn (generador de candidatos).
STRATEGIES = [
    {"name": "PED", "universe": MEGA_CAP_PED, "needs": "recent_earnings", "fn": ped_candidates},
]


def all_universe():
    """Unión de los universos de todas las estrategias (para un solo fetch de barras)."""
    u = set()
    for s in STRATEGIES:
        u |= set(s["universe"])
    return u
