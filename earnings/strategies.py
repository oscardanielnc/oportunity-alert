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
