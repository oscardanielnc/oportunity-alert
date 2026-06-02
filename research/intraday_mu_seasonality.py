import os, sys, math, statistics
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import requests

#!/usr/bin/env python3
"""
ANÁLISIS — "Trade alrededor del core": ¿hay horas/niveles explotables en MU intradía?
=====================================================================================
Idea de Oscar (2026-06-02): mientras una posición de Marea (ej. MU) no toque su chandelier 4×ATR,
sube y baja. ¿Conviene comprar pullbacks y vender máximos alrededor del core? Para eso quería saber
(a) a qué HORAS MU suele estar alto/bajo, (b) mapear soportes/resistencias diarios para saber dónde
comprar/vender. Esto lo MIDE con datos reales (no de palabra), con el spread de eToro como valla.

TENSIÓN previa (de la propia investigación): vender máximos = vender FUERZA, y recortar ganadores
restó en Estudios A/C-derisk; y el scalping intradía murió por fees (Watcher). Prior negativo, pero
medimos por si hay asimetría rescatable (¿los dips sí son comprables aunque vender rips pierda?).

VALLA DE COSTO: eToro acciones reales = sin comisión pero SÍ spread. Para MU (líquida) ~0.05-0.10%
por lado → round-trip ~0.15%. Un patrón solo es tradeable si el movimiento PREDECIBLE supera ~0.15%.

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02, MU, 188 días intradía + 606 diarios) — VEREDICTO: NO conviene comprar dips/
vender rips sobre el core. Los datos contradicen la mean-reversion; MU es momentum. Hay aprendizajes.

1. HORARIO: 09:30 es 50/50 (máximo del día 47% / mínimo 46% → moneda al aire). Camino típico baja
   ~10:00 y deriva al cierre (~15:30 lo más alto): comprar 10:00/vender 15:30 = +0.284% bruto > 0.15%
   spread EN PAPEL. PERO es ESPEJISMO: una sola ventana de 9 meses en bull; la "deriva al cierre" es la
   tendencia alcista filtrándose; autocorrelación intradía ~0 (−0.009 = sin memoria, los dips NO rebotan);
   y como la posición YA se tiene, esa deriva ya se captura → operarla encima solo suma costo/riesgo.
2. EL DATO QUE MATA "vender rips" (606 días, robusto): retorno del día SIGUIENTE a una BAJA +0.353% vs
   siguiente a una SUBA +0.617%. MU rinde MÁS tras subir → MOMENTUM, no reversión. Vender el rip = vender
   antes de la continuación más grande. La idea aplicada a MU está AL REVÉS. Confirma "no cortar ganadores".

RESCATE (3): (a) aprendizaje: mantener el core + chandelier es lo correcto en momentum; no operar contra
la tendencia. (b) timing de EJECUCIÓN de una decisión ya tomada: evitar la apertura (9:30 caótica), leve
deriva al cierre (coincide con PED intraday D2). (c) soportes/resistencias en la card = útil para mejor
ENTRADA / ver invalidación, NO para churnear (extiende la Fase 1 de niveles a posiciones abiertas).
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
ET = ZoneInfo("America/New_York")
H = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
COST_RT = 0.15   # % round-trip estimado en eToro (2× spread)


def fetch(tk, tf, start):
    bars, params = [], {"timeframe": tf, "start": start, "feed": "sip", "limit": 10000,
                        "sort": "asc", "adjustment": "all"}
    while True:
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{tk}/bars", params=params, headers=H, timeout=40)
        r.raise_for_status(); j = r.json()
        bars.extend(j.get("bars") or [])
        if not j.get("next_page_token"): break
        params["page_token"] = j["next_page_token"]
    return bars


def main():
    tk = sys.argv[1] if len(sys.argv) > 1 else "MU"
    print(f"\n{'='*92}\n  TRADE-ALREDEDOR-DEL-CORE — {tk} | spread eToro asumido {COST_RT}% round-trip\n{'='*92}")

    # ── INTRADÍA: 30-min, sesión regular (9:30-16:00 ET) ──
    raw = fetch(tk, "30Min", "2025-09-01T00:00:00Z")
    days = {}
    for b in raw:
        et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        hm = et.hour * 60 + et.minute
        if 9 * 60 + 30 <= hm <= 15 * 60 + 30:           # barras que ABREN en sesión regular
            days.setdefault(et.date(), []).append((et.strftime("%H:%M"), b))
    days = {d: sorted(v) for d, v in days.items() if len(v) >= 12}
    print(f"  Intradía: {len(days)} días de sesión (30-min), {raw[0]['t'][:10]} → {raw[-1]['t'][:10]}")

    slots = [f"{9 + (30 + 30*i)//60:02d}:{(30 + 30*i)%60:02d}" for i in range(13)]
    ret_by_slot = {s: [] for s in slots}               # retorno intrabarra (close/open-1)
    path_by_slot = {s: [] for s in slots}              # precio normalizado al open del día
    high_slot = {s: 0 for s in slots}; low_slot = {s: 0 for s in slots}
    ranges = []
    for d, rows in days.items():
        o0 = rows[0][1]["o"]
        if o0 <= 0: continue
        hs = max(rows, key=lambda x: x[1]["h"]); ls = min(rows, key=lambda x: x[1]["l"])
        high_slot[hs[0]] = high_slot.get(hs[0], 0) + 1
        low_slot[ls[0]]  = low_slot.get(ls[0], 0) + 1
        ranges.append((max(r[1]["h"] for r in rows) - min(r[1]["l"] for r in rows)) / o0 * 100)
        for s, b in rows:
            if s in ret_by_slot:
                ret_by_slot[s].append((b["c"]/b["o"]-1)*100)
                path_by_slot[s].append((b["c"]/o0-1)*100)

    print(f"\n  Rango intradía medio (high-low)/open: {statistics.mean(ranges):.2f}%  "
          f"(mediana {statistics.median(ranges):.2f}%)  -> 'espacio' bruto para operar")
    print(f"\n  {'Slot ET':>7} {'ret medio%':>10} {'t-stat':>7} {'precio vs open%':>15} {'%días HIGH':>11} {'%días LOW':>10}")
    print(f"  {'-'*64}")
    nD = len(days)
    for s in slots:
        rs = ret_by_slot[s]; ps = path_by_slot[s]
        if not rs: continue
        mu = statistics.mean(rs); sd = statistics.pstdev(rs) or 1e-9
        t = mu/(sd/math.sqrt(len(rs)))
        print(f"  {s:>7} {mu:>+9.3f}% {t:>7.2f} {statistics.mean(ps):>+14.3f}% "
              f"{high_slot[s]/nD*100:>10.0f}% {low_slot[s]/nD*100:>9.0f}%")

    # edge horario: gap entre el slot más barato y el más caro del "camino típico"
    avgpath = {s: statistics.mean(path_by_slot[s]) for s in slots if path_by_slot[s]}
    lo_s = min(avgpath, key=avgpath.get); hi_s = max(avgpath, key=avgpath.get)
    gap = avgpath[hi_s] - avgpath[lo_s]
    print(f"\n  Camino típico: más BAJO ~{lo_s} ({avgpath[lo_s]:+.3f}%), más ALTO ~{hi_s} ({avgpath[hi_s]:+.3f}%)")
    print(f"  Edge horario bruto (comprar {lo_s} / vender {hi_s}): {gap:.3f}%  vs valla {COST_RT}% -> "
          f"{'TRADEABLE' if gap > COST_RT else 'NO supera el spread'}")

    # autocorrelación intradía (¿revierte o trendea dentro del día?)
    seq = []
    for d, rows in days.items():
        for s, b in rows: seq.append(b["c"]/b["o"]-1)
    ac1 = _autocorr(seq, 1)
    print(f"  Autocorrelación 30-min (lag 1): {ac1:+.3f}  "
          f"({'revierte (dip comprable)' if ac1 < -0.02 else 'trendea (dip NO rebota)' if ac1 > 0.02 else 'ruido (sin memoria)'})")

    # ── DIARIO: ¿comprar tras caída / vender tras subida agrega? ──
    daily = fetch(tk, "1Day", "2024-01-01T00:00:00Z")
    cl = [b["c"] for b in daily]
    rets = [(cl[i]/cl[i-1]-1)*100 for i in range(1, len(cl))]
    ac1d = _autocorr(rets, 1)
    after_down = [rets[i] for i in range(1, len(rets)) if rets[i-1] < 0]
    after_up   = [rets[i] for i in range(1, len(rets)) if rets[i-1] > 0]
    print(f"\n  DIARIO ({len(daily)} días desde 2024):")
    print(f"  Autocorrelación diaria (lag 1): {ac1d:+.3f}  "
          f"({'revierte' if ac1d < -0.03 else 'trendea (momentum)' if ac1d > 0.03 else 'ruido'})")
    print(f"  Retorno medio del día SIGUIENTE a una BAJA: {statistics.mean(after_down):+.3f}%  | "
          f"siguiente a una SUBA: {statistics.mean(after_up):+.3f}%")
    print(f"  -> Si 'tras baja' >> 'tras suba' y supera {COST_RT}%, comprar dips/vender rips tendría base.")

    print(f"\n{'='*92}\n")


def _autocorr(x, lag):
    n = len(x)
    if n <= lag + 2: return 0.0
    m = statistics.mean(x); v = sum((xi-m)**2 for xi in x)
    c = sum((x[i]-m)*(x[i-lag]-m) for i in range(lag, n))
    return c/v if v else 0.0


if __name__ == "__main__":
    main()
