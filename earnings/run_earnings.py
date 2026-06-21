#!/usr/bin/env python3
"""
Runner diario de la sección EARNINGS — genera candidatos por estrategia + calendario de
próximos earnings, los registra en el Scoreboard (arm='earnings') y emite el panel del dashboard.

NO simula trades (sin paper-portfolio): solo surface señales. Oscar decide manualmente.

Correr UNA vez al día tras el cierre de USA (igual que el piloto):
    python -m earnings.run_earnings              # corre + registra en scoreboard + dashboard
    python -m earnings.run_earnings --no-scoreboard   # solo consola + dashboard (sin registrar)

Estado/panel: data/earnings_dashboard.json
"""
import os, sys, json, argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pilot.momentum_signals import fetch_daily_batch
from pilot.ped_signals import fetch_earnings_map
from earnings.strategies import STRATEGIES, all_universe
from earnings.calendar import upcoming_earnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DASH_PATH = os.path.join(DATA_DIR, "earnings_dashboard.json")
DB_PATH = os.path.join(DATA_DIR, "metrics.db")
CALENDAR_DAYS = 14


def run(use_scoreboard=True):
    universe = sorted(all_universe())
    data = fetch_daily_batch(universe + ["QQQ"])
    qqq = data.pop("QQQ", [])
    if not qqq:
        print("[earnings] sin datos de QQQ — abortando"); return None
    ref_date = qqq[-1]["t"]

    # Mapa de earnings YA reportados (para estrategias reactivas tipo PED).
    earnings_map = fetch_earnings_map(universe)
    _with = sum(1 for t in universe if earnings_map.get(t))
    print(f"[earnings] earnings recientes detectados en {_with}/{len(universe)} tickers")

    # Candidatos de todas las estrategias.
    candidates = []
    for strat in STRATEGIES:
        try:
            cands = strat["fn"](data, earnings_map, ref_date)
        except Exception as e:
            print(f"[earnings][WARN] estrategia {strat['name']} falló: {e}"); cands = []
        candidates.extend(cands)
        print(f"[earnings] {strat['name']}: {len(cands)} candidato(s)"
              + (f" -> {[c['ticker'] for c in cands]}" if cands else ""))

    # Calendario de próximos earnings (base del pre-posicionamiento).
    calendar = upcoming_earnings(universe, days_ahead=CALENDAR_DAYS)
    print(f"[earnings] próximos {CALENDAR_DAYS}d: {len(calendar)} earnings agendados")

    # Registrar candidatos en el Scoreboard (arm='earnings'). Idempotente por (arm,ticker,ts):
    # t0 anclado a ref_date 20:00 UTC (post-cierre) => re-correr el mismo día NO duplica.
    if use_scoreboard and candidates:
        try:
            from utils import scoreboard
            scoreboard.ensure_table(DB_PATH)
            t0_iso = f"{ref_date}T20:00:00+00:00"
            for c in candidates:
                scoreboard.record_signal(
                    DB_PATH, "earnings", c["ticker"], c["direction"], c["category"],
                    int(round(c["conviction"])) if c.get("conviction") is not None else None,
                    f"finnhub_earnings:{c['strategy']}", None, t0_iso, c.get("price_t0"),
                    scope="ticker",
                )
            print(f"[earnings] {len(candidates)} candidato(s) registrados en scoreboard")
        except Exception as e:
            print(f"[earnings][WARN] scoreboard: {e}")

    dash = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "as_of": ref_date,
        "strategies": [
            {"name": s["name"],
             "candidates": [c for c in candidates if c["strategy"] == s["name"]]}
            for s in STRATEGIES
        ],
        "calendar": calendar,
        "calendar_days": CALENDAR_DAYS,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DASH_PATH, "w", encoding="utf-8") as f:
        json.dump(dash, f, indent=2, ensure_ascii=False)
    print(f"[earnings] dashboard -> {DASH_PATH}")
    return dash


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scoreboard", action="store_true", help="no registrar en scoreboard")
    args = ap.parse_args()
    run(use_scoreboard=not args.no_scoreboard)
