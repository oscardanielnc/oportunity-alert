import os, sys, json, statistics
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
EJECUCIÓN — ¿a qué hora COMPRAR/VENDER las señales de Marea? (no es estrategia, es timing de fill)
=================================================================================================
Oscar (2026-06-02): le advertí no comprar en la apertura (9:30 ET = 8:30am Lima) por volatilidad.
Pregunta justa: entonces ¿a qué hora? ¿antes o después de la apertura? Con datos, y no solo MU sino
TODO el universo de Marea (top-80), para que la hora sea robusta.

OJO conceptual: la apertura es VOLÁTIL (mal fill posible), pero eso NO significa que sea mal PRECIO.
Son cosas distintas. Medimos el CAMINO intradía promedio normalizado a la apertura (9:30=0%): si el
precio deriva hacia arriba, la apertura es el precio más BARATO -> comprar temprano es bueno (y comprar
al open es lo que el backtest valida -> no erosiona el edge). Si deriva hacia abajo, conviene esperar.

Condición de ENTRADA de Marea: breakouts, suelen abrir con GAP-UP. Por eso medimos también el camino
SOLO en días de gap-up (open>cierre previo), que se parece más a cómo entra Marea de verdad.

VALLA: eToro acciones = spread (~0.1-0.15% round-trip). Y nota: eToro normalmente NO opera pre-market
de acciones para retail -> el "antes de la apertura" puede no ser ejecutable (Oscar lo confirma).

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02, 80 tickers, 14,823 ticker-días) — el precio DERIVA AL ALZA durante el día
(+0.137% open->cierre). Camino normalizado a la apertura (9:30 ET = 8:30 Lima):
  08:30 Lima (open) +0.047% | 09:00 +0.021% (más BARATO) | 13:00 +0.11% | 14:30 (cierre) +0.137% (más CARO)
  Pre-market ~08:00 Lima: -0.013% (≈ nada, y eToro no lo opera). Gap overnight +0.228% (ya ocurrió).
  Rango del primer bar (8:30): 2.59% = el más volátil del día.

RECOMENDACIÓN:
  COMPRAR -> TEMPRANO, 8:30-9:00 Lima, orden LÍMITE cerca del open, evitando el primer minuto (el
    'no comprar al open' era por VOLATILIDAD del primer bar, no por precio; el open es el más barato).
    Esperar al mediodía/tarde hace PAGAR MÁS (+0.1%). No hay pullback que esperar: deriva al alza.
  VENDER -> TARDE, ~13:30-14:30 Lima (el precio suele estar más alto cerca del cierre).
HONESTIDAD: los números son chicos (~0.1%, dentro del spread por trade) y la deriva es en parte de
este periodo alcista. No mueve retornos; solo evita perjudicarse (comprar tarde / vender en el caos
del open). Coincide con el backtest (compra al open) -> no erosiona el edge.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
ET = ZoneInfo("America/New_York")
H = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_intraday30_cache.json")
SLOTS = [f"{9 + (30 + 30*i)//60:02d}:{(30 + 30*i)%60:02d}" for i in range(13)]   # 09:30..15:30


def fetch_30m(tk, cache):
    if tk in cache:
        return cache[tk]
    bars, params = [], {"timeframe": "30Min", "start": "2025-09-01T00:00:00Z", "feed": "sip",
                        "limit": 10000, "sort": "asc", "adjustment": "all"}
    try:
        while True:
            r = requests.get(f"https://data.alpaca.markets/v2/stocks/{tk}/bars", params=params, headers=H, timeout=40)
            r.raise_for_status(); j = r.json()
            bars.extend(j.get("bars") or [])
            if not j.get("next_page_token"): break
            params["page_token"] = j["next_page_token"]
    except Exception as e:
        print(f"  err {tk}: {e}")
    # comprimir a lo necesario: (ET hh:mm, o, h, l, c, date)
    out = []
    for b in bars:
        et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        out.append([et.strftime("%H:%M"), str(et.date()), b["o"], b["h"], b["l"], b["c"]])
    cache[tk] = out
    return out


def main():
    uni = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "data", "pilot_universe.json")))["tickers"]
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    n0 = len(cache)
    print(f"\n{'='*96}\n  TIMING DE EJECUCIÓN — universo Marea ({len(uni)}) | 30-min | camino vs apertura 9:30 ET\n{'='*96}")

    path_all = {s: [] for s in SLOTS}            # close[slot]/open_0930 - 1, todos los días
    path_gap = {s: [] for s in SLOTS}            # solo días gap-up (open>cierre previo)
    open_range, premkt_vs_open, gap_overnight = [], [], []
    ndays = 0

    for ti, tk in enumerate(uni):
        bars = fetch_30m(tk, cache)
        # agrupar por día
        byday = {}
        for hm, d, o, h, l, c in bars:
            byday.setdefault(d, []).append((hm, o, h, l, c))
        dates = sorted(byday)
        prev_reg_close = {}
        for k, d in enumerate(dates):
            rows = sorted(byday[d])
            reg = [r for r in rows if r[0] in SLOTS]
            if len(reg) < 12 or reg[0][0] != "09:30":
                continue
            open0 = reg[0][1]
            if open0 <= 0:
                continue
            # pre-market: bar ~09:00 ET
            pm = [r for r in rows if r[0] == "09:00"]
            if pm:
                premkt_vs_open.append((pm[0][4] / open0 - 1) * 100)
            # gap overnight vs cierre regular previo
            pc = prev_reg_close.get(tk)
            gap_up = None
            if pc:
                g = (open0 / pc - 1) * 100; gap_overnight.append(g); gap_up = g > 0
            open_range.append((reg[0][2] - reg[0][3]) / open0 * 100)   # rango del bar 9:30
            for hm, o, h, l, c in reg:
                if hm in path_all:
                    path_all[hm].append((c / open0 - 1) * 100)
                    if gap_up:
                        path_gap[hm].append((c / open0 - 1) * 100)
            prev_reg_close[tk] = reg[-1][4]
            ndays += 1
        if (ti + 1) % 20 == 0:
            print(f"  ... {ti+1}/{len(uni)} tickers")

    if len(cache) != n0:
        json.dump(cache, open(CACHE, "w"))

    def avg(d, s): return statistics.mean(d[s]) if d[s] else 0.0

    print(f"\n  {ndays:,} ticker-días | rango medio bar 9:30: {statistics.mean(open_range):.2f}% "
          f"(el más volátil del día)")
    print(f"\n  {'Slot ET':>7} {'Lima':>7} {'precio vs 9:30 (todos)':>22} {'precio vs 9:30 (gap-up)':>23}")
    print(f"  {'-'*62}")
    for s in SLOTS:
        hh, mm = s.split(":"); lima = f"{int(hh)-1:02d}:{mm}"
        print(f"  {s:>7} {lima:>7} {avg(path_all, s):>+20.3f}% {avg(path_gap, s):>+22.3f}%")

    # mínimo y máximo del camino típico
    pa = {s: avg(path_all, s) for s in SLOTS}
    lo, hi = min(pa, key=pa.get), max(pa, key=pa.get)
    print(f"\n  Camino típico (todos): más BARATO ~{lo} ({pa[lo]:+.3f}%) | más CARO ~{hi} ({pa[hi]:+.3f}%)")
    print(f"  Open->cierre (15:30): {pa['15:30']:+.3f}%  -> "
          f"{'el precio DERIVA AL ALZA durante el día (comprar TEMPRANO conviene)' if pa['15:30'] > 0.05 else 'sin deriva clara' if abs(pa['15:30']) <= 0.05 else 'deriva a la baja (esperar conviene)'}")
    print(f"\n  Pre-market (~09:00 ET / 08:00 Lima) vs apertura: {statistics.mean(premkt_vs_open):+.3f}% "
          f"(negativo = pre-market más barato que el open; eToro casi nunca deja operarlo)")
    print(f"  Gap overnight medio (open vs cierre previo): {statistics.mean(gap_overnight):+.3f}%")

    print(f"\n{'='*96}")
    print("  Lectura: si el camino es POSITIVO y creciente, la apertura es el precio más barato del día")
    print("  (comprar al open = bueno y = lo que valida el backtest). El consejo 'no comprar al open' es")
    print("  por VOLATILIDAD del primer bar, no por precio: usar la 1ª media hora con orden límite cerca")
    print("  del open, evitando el primer minuto. Para VENDER: el precio suele estar más alto al cierre.")
    print(f"{'='*96}\n")


if __name__ == "__main__":
    main()
