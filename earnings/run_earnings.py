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

from datetime import timedelta

from pilot.momentum_signals import fetch_daily_batch
from pilot.ped_signals import fetch_earnings_map, PED_HOLD_DAYS
from earnings.strategies import (STRATEGIES, all_universe, prerun_for, PRERUN_PROJ,
                                 PRERUN_ENTRY_CAL_DAYS)
from earnings.calendar import upcoming_earnings

def _next_session_iso(ref_date):
    """Apertura (~13:30 UTC ≈ 9:30 ET) de la PRÓXIMA sesión tras ref_date — la ENTRADA de PED
    (Day+2 open, siendo ref_date el día de reacción Day+1). Salta sáb/dom (ignora feriados;
    el resolver del scoreboard ancla a la primera barra real ≥ t0, así que un feriado solo
    corre el ancla al siguiente open)."""
    d = datetime.strptime(ref_date, "%Y-%m-%d").date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return f"{d.isoformat()}T13:30:00+00:00"


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DASH_PATH = os.path.join(DATA_DIR, "earnings_dashboard.json")
DB_PATH = os.path.join(DATA_DIR, "metrics.db")
CALENDAR_DAYS = 14
# Horizonte de medición del trade PED en el scoreboard: hold = PED_HOLD_DAYS ruedas desde la
# entrada (Day+2) → a calendario ≈ *7/5. Cubre Day+2 → ~Day+8 para medir el drift completo al exit.
PED_HORIZON_H = round(PED_HOLD_DAYS * (7 / 5) * 24)   # 6 ruedas ≈ 202h ≈ 8.4 días


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

    # PRE-RUN-UP: anotar cada earning próximo con la recomendación (no se duplica como candidato:
    # vive sobre el calendario). applies = nombre sobre su SMA50; entry_date ≈ 5 ruedas antes.
    prerun_open = []   # señales a registrar (ventana de entrada abierta)
    for e in calendar:
        pr = prerun_for(e["ticker"], e["days_until"], data.get(e["ticker"]))
        if not pr:
            continue
        rep = datetime.strptime(e["date"], "%Y-%m-%d").date()
        entry_date = (rep - timedelta(days=PRERUN_ENTRY_CAL_DAYS))
        pr["entry_date"] = entry_date.isoformat()
        eid = pr["entry_in_days"]
        if not pr["applies"]:
            pr["status"], pr["label"] = "skip", "No aplica (bajo SMA50)"
        elif eid > 1:
            pr["status"], pr["label"] = "upcoming", f"Entrar PRE en ~{eid}d (≈{pr['entry_date']})"
        elif eid >= -1:
            pr["status"], pr["label"] = "enter_now", "🟢 ENTRAR PRE ahora (ventana abierta)"
        else:
            pr["status"], pr["label"] = "holding", "Ventana de entrada pasó — mantener hasta el reporte"
        e["prerun"] = pr
        if pr["applies"] and pr["status"] == "enter_now" and e["days_until"] >= 1:
            prerun_open.append((e, pr, data.get(e["ticker"])))
    _pr_apply = sum(1 for e in calendar if e.get("prerun", {}).get("applies"))
    print(f"[earnings] pre-run-up: {_pr_apply}/{len(calendar)} próximos aplican (sobre SMA50)"
          + (f", {len(prerun_open)} en ventana de entrada" if prerun_open else ""))

    # Registrar candidatos en el Scoreboard (arm='earnings') — el track arranca el día que se
    # ACTIVA la entrada de cada trade. Idempotente por (arm,ticker,ts).
    #   PED: entrada Day+2 open -> t0 = próxima sesión (price_t0 lo fija el resolver al open).
    #   Pre-run-up: entrada al abrir la ventana -> t0 = entry_date (más abajo).
    if use_scoreboard and (candidates or prerun_open):
        try:
            from utils import scoreboard
            scoreboard.ensure_table(DB_PATH)
            ped_entry_iso = _next_session_iso(ref_date)
            for c in candidates:
                scoreboard.record_signal(
                    DB_PATH, "earnings", c["ticker"], c["direction"], c["category"],
                    int(round(c["conviction"])) if c.get("conviction") is not None else None,
                    f"finnhub_earnings:{c['strategy']}", None, ped_entry_iso, None,
                    scope="ticker", horizon_h=PED_HORIZON_H,
                )
            # Pre-run-up: registrar al ABRIR la ventana (idempotente por entry_date). Horizonte =
            # entrada → víspera del reporte (último cierre antes del earning) para NO medir el evento.
            for e, pr, bars in prerun_open:
                px = bars[-1]["c"] if bars else None
                rep = datetime.strptime(e["date"], "%Y-%m-%d").date()
                exit_d = rep if e["hour"] == "amc" else rep - timedelta(days=1)
                en_d = datetime.strptime(pr["entry_date"], "%Y-%m-%d").date()
                horizon = max(24.0, (exit_d - en_d).days * 24.0)
                scoreboard.record_signal(
                    DB_PATH, "earnings", e["ticker"], "LONG", "prerun", None,
                    "prerun:calendar", None, f"{pr['entry_date']}T20:00:00+00:00", px,
                    scope="ticker", horizon_h=horizon,
                )
            print(f"[earnings] {len(candidates)} candidato(s) PED + {len(prerun_open)} "
                  f"pre-run-up registrados en scoreboard")
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
        "prerun_proj": PRERUN_PROJ,    # proyección histórica del pre-run-up (para el calendario)
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
