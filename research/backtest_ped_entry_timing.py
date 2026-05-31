#!/usr/bin/env python3
"""
PED — ¿QUÉ DÍA / HORA es mejor ENTRAR?  (estudio de evento)

Hoy PED entra al OPEN de Day+2 (miércoles, si earnings AMC del lunes → reacción martes).
Oscar (2026-05-31): la señal ya se CONFIRMA al cierre del martes (la reacción ≥+5% es
close-to-close de ese día). Si está en rally, esperar al miércoles-open puede hacerte
entrar MÁS CARO. ¿Conviene entrar martes al cierre? ¿miércoles al cierre? ¿martes al open?

Este script NO cambia la SEÑAL (mismos filtros validados: mega-cap, reacción ≥+5% y ≤+25%,
sin gap-down >5%). Solo varía el PUNTO DE ENTRADA y mide el retorno hasta una SALIDA COMÚN
(Day+7 close) para aislar el efecto del precio de entrada (misma ventana de drift para todos).

Entradas comparadas (todas causalmente válidas: la reacción se conoce al cierre de r):
  - reac_close : CIERRE del día de reacción (martes) — apenas confirma la señal.
  - d2_open    : OPEN de Day+2 (miércoles) — REGLA ACTUAL.
  - d2_close   : CIERRE de Day+2 (miércoles).
  - d3_open    : OPEN de Day+3 (jueves).
Variante con disparador distinto (gap conocido al open):
  - reac_open  : OPEN del día de reacción (martes), condicionado a GAP de apertura ≥+5%.

Comisión: idéntica (1 round-trip) en todas las variantes → no cambia el ranking; se compara
retorno GROSS por trade. Reusa super_backtest.fetch_all + el universo/earnings de backtest_ped.
"""
import os, sys, json, time, statistics
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_ped import UNIVERSE, fetch_earnings_events, WEEKS
from pilot.ped_signals import PED_THRESHOLD, PED_PRICED_CAP, PED_GAP_FLOOR

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ped_timing_cache.json")
EXIT_OFFSET = 6     # salida común = bars[r+6]["c"] = Day+7 close (mismo que pnl_d7 de backtest_ped)


def extract_events(start_iso, sd):
    """Para cada earnings, guarda todos los precios necesarios para comparar entradas."""
    evs_out = []
    for tk in UNIVERSE:
        bars = fetch_all(tk, "1Day", start_iso, limit=2000)
        if len(bars) < 30:
            print(f"  {tk:<6} sin datos"); continue
        bars = [{"t": b["t"][:10], "o": float(b["o"]), "h": float(b["h"]),
                 "l": float(b["l"]), "c": float(b["c"])} for b in bars]
        dates = [b["t"] for b in bars]; idx = {d: i for i, d in enumerate(dates)}
        events = fetch_earnings_events(tk, sd, datetime.now().strftime("%Y-%m-%d"))
        time.sleep(0.15)
        n = 0
        for ev in events:
            E, h = ev["date"], ev["hour"]
            if E not in idx: continue
            ei = idx[E]; r = ei if h == "bmo" else ei + 1
            if r - 1 < 0 or r + EXIT_OFFSET >= len(bars) or r + 2 >= len(bars):
                continue
            pre = bars[r-1]["c"]
            if pre <= 0 or bars[r]["c"] <= 0 or bars[r+1]["o"] <= 0:
                continue
            evs_out.append({
                "ticker": tk, "date": E, "hour": h,
                "day1_ret": (bars[r]["c"]-pre)/pre*100,
                "gap":      (bars[r]["o"]-pre)/pre*100,
                "reac_open":  bars[r]["o"], "reac_close": bars[r]["c"],
                "d2_open":    bars[r+1]["o"], "d2_close": bars[r+1]["c"],
                "d3_open":    bars[r+2]["o"],
                "exit_px":    bars[r+EXIT_OFFSET]["c"],
            })
            n += 1
        print(f"  {tk:<6} {len(events)} earnings -> {n} eventos")
    return evs_out


def qualifies(e):
    """Mismos filtros PED validados (close-to-close)."""
    return (e["day1_ret"] >= PED_THRESHOLD and e["day1_ret"] <= PED_PRICED_CAP
            and e["gap"] >= PED_GAP_FLOOR)


def _stats(rets):
    if not rets: return None
    wr = sum(1 for r in rets if r > 0)/len(rets)*100
    return {"n": len(rets), "mean": statistics.mean(rets),
            "med": statistics.median(rets), "wr": wr}


def _ret(e, entry_key):
    p = e[entry_key]
    return (e["exit_px"] - p)/p*100 if p > 0 else None


def main():
    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    print(f"\n{'#'*96}")
    print(f"  PED — TIMING DE ENTRADA  |  {len(UNIVERSE)} large-caps | {WEEKS} sem | salida común Day+7 close")
    print(f"{'#'*96}")

    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        evs = json.load(open(CACHE, encoding="utf-8"))
        print(f"  (cache: {len(evs)} eventos — usar --fresh para recomputar)")
    else:
        evs = extract_events(start_iso, sd)
        json.dump(evs, open(CACHE, "w", encoding="utf-8"))

    qual = [e for e in evs if qualifies(e)]
    print(f"\n  Eventos PED que califican (reac +{PED_THRESHOLD:.0f}..+{PED_PRICED_CAP:.0f}%, "
          f"gap>={PED_GAP_FLOOR:.0f}%): {len(qual)} de {len(evs)} earnings totales")

    # ── 1) ¿Sigue subiendo de reac-close (martes) a Day+2 open (miércoles)? ──────
    print(f"\n{'='*96}")
    print(f"  1) ¿ESPERAR AL OPEN DE Day+2 TE HACE ENTRAR MÁS CARO?  (segmentos overnight/intradía)")
    print(f"{'='*96}")
    seg_reac_to_d2o = [(e["d2_open"]-e["reac_close"])/e["reac_close"]*100 for e in qual]
    seg_d2o_to_d2c  = [(e["d2_close"]-e["d2_open"])/e["d2_open"]*100 for e in qual]
    seg_reac_to_d3o = [(e["d3_open"]-e["reac_close"])/e["reac_close"]*100 for e in qual]
    for lab, seg in [("reac_close (mar) -> Day+2 open (mié)", seg_reac_to_d2o),
                     ("Day+2 open -> Day+2 close (intradía mié)", seg_d2o_to_d2c),
                     ("reac_close (mar) -> Day+3 open (jue)", seg_reac_to_d3o)]:
        s = _stats(seg)
        print(f"  {lab:<42} media {s['mean']:>+6.2f}%  mediana {s['med']:>+6.2f}%  "
              f"%subió {sum(1 for x in seg if x>0)/len(seg)*100:>3.0f}%")
    print(f"\n  Si 'reac_close -> Day+2 open' es POSITIVO en media -> la accion sube de noche y")
    print(f"  esperar al miércoles-open te hace pagar MÁS (entrar martes-cierre sería más barato).")

    # ── 2) Retorno hasta salida común (Day+7 close) por punto de entrada ─────────
    print(f"\n{'='*96}")
    print(f"  2) RETORNO HASTA SALIDA COMÚN (Day+7 close) — ¿qué entrada captura más drift?")
    print(f"{'='*96}")
    print(f"  {'Entrada':<28} {'n':>4} {'media%':>8} {'mediana%':>9} {'WR%':>6} {'vs Day+2 open':>14}")
    print(f"  {'-'*78}")
    ref = _stats([_ret(e, "d2_open") for e in qual])
    rows = [("reac_close (mar cierre)", "reac_close"),
            ("d2_open (mié open) ACTUAL", "d2_open"),
            ("d2_close (mié cierre)", "d2_close"),
            ("d3_open (jue open)", "d3_open")]
    for lab, key in rows:
        s = _stats([_ret(e, key) for e in qual])
        delta = s["mean"] - ref["mean"]
        print(f"  {lab:<28} {s['n']:>4} {s['mean']:>+7.2f}% {s['med']:>+8.2f}% {s['wr']:>5.0f}% "
              f"{delta:>+13.2f}%")
    print(f"\n  'media%' = retorno gross por trade hasta Day+7 close. Comisión idéntica en todas")
    print(f"  (no cambia el ranking). 'vs Day+2 open' = cuánto MEJOR/peor que la regla actual.")

    # ── 3) Variante disparador-gap: entrar martes al OPEN si gap apertura ≥+5% ───
    print(f"\n{'='*96}")
    print(f"  3) VARIANTE 'martes al OPEN' (disparador = GAP de apertura >=+5%, distinto a la senal close)")
    print(f"{'='*96}")
    gap_set = [e for e in evs if e["gap"] >= PED_THRESHOLD and e["gap"] <= PED_PRICED_CAP]
    sg = _stats([_ret(e, "reac_open") for e in gap_set])
    sc = _stats([_ret(e, "reac_close") for e in gap_set])
    print(f"  Eventos con gap apertura >=+{PED_THRESHOLD:.0f}%: {len(gap_set)}")
    if sg:
        print(f"  Entrar reac_open (martes apertura)  n={sg['n']:>3} media {sg['mean']:>+6.2f}%  "
              f"mediana {sg['med']:>+6.2f}%  WR {sg['wr']:.0f}%")
        print(f"  (mismos eventos) reac_close cierre   n={sc['n']:>3} media {sc['mean']:>+6.2f}%  "
              f"mediana {sc['med']:>+6.2f}%  WR {sc['wr']:.0f}%")
        print(f"  Dif open-vs-close del día de reacción: {sg['mean']-sc['mean']:>+.2f}% media")
    print(f"\n  Lectura global: la entrada con mayor media% Y buen WR, sin asumir más riesgo, gana.\n")


if __name__ == "__main__":
    main()
