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
PRE-MARKET del TOP-10 — ¿conviene comprar antes de la apertura? (Oscar: eToro le deja, y lo ve más barato)
========================================================================================================
Oscar (2026-06-02): observó que el top-10 antes de la apertura está un poco más abajo que a las 9am Lima,
y eToro le deja operar pre-market casi todos. Mide específicamente el TOP-10 vigente con 15-min:
  - precio pre-market justo antes del open (09:15 ET = 08:15 Lima) vs apertura (09:30 ET = 08:30 Lima)
    vs 9am Lima (10:00 ET).
  - LIQUIDEZ/spread del pre-market (volumen + rango del bar) = el detalle que decide si el 'más barato'
    es real o se lo come el spread ancho del pre-market.
Horario: junio = EDT (UTC-4). 9:30 ET = 8:30 Lima. 'a las 9am Lima' = 10:00 ET.

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02, top-10 SNDK/AAOI/MU/DELL/WDC/STX/ARM/INTC/LITE/RKLB, ~188 días c/u):
  pre-market (08:15 Lima) vs apertura: −0.004% (≈ IGUALES) | vs 9am Lima: −0.299% (pre más barato)
  volumen pre-market / apertura: 0.03x (2-4% → ILÍQUIDO, spread ANCHO)

VEREDICTO: Oscar tiene razón en que el pre-market está ~0.3% bajo las 9am Lima — PERO está IGUAL que
la apertura (−0.004%). Esa ventaja de 0.3% NO es del pre-market: es la deriva al alza después del open
(que también capturás comprando al open). El pre-market no da descuento sobre la apertura. Y como es
ilíquido (2-4% del volumen → spread ancho), el FILL en la apertura (8:30 Lima) suele ser MEJOR a igual
mid-price. Factor decisor = el spread REAL de eToro por nombre (no medible acá): si pre-market ≈ regular,
da igual; si es más ancho (típico), comprar al open. Regla robusta: comprar TEMPRANO (pre-market u 8:30
Lima, lo que dé mejor spread), NUNCA esperar a las 9am (+0.3%). Coincide con intraday_execution_timing.py.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
ET = ZoneInfo("America/New_York")
H = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
TOP10 = ["SNDK", "AAOI", "MU", "DELL", "WDC", "STX", "ARM", "INTC", "LITE", "RKLB"]


def fetch_15m(tk):
    bars, params = [], {"timeframe": "15Min", "start": "2025-09-01T00:00:00Z", "feed": "sip",
                        "limit": 10000, "sort": "asc", "adjustment": "all"}
    while True:
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{tk}/bars", params=params, headers=H, timeout=40)
        r.raise_for_status(); j = r.json()
        bars.extend(j.get("bars") or [])
        if not j.get("next_page_token"): break
        params["page_token"] = j["next_page_token"]
    out = []
    for b in bars:
        et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        out.append((et.strftime("%H:%M"), str(et.date()), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)))
    return out


def main():
    print(f"\n{'='*100}")
    print(f"  PRE-MARKET TOP-10 — ¿comprar antes de la apertura? | 15-min | norm. a la apertura (9:30 ET=8:30 Lima)")
    print(f"{'='*100}")
    print(f"  {'Ticker':>6} {'pre 08:15L':>11} {'9am Lima':>9} {'pre vs 9am':>11} {'pre/reg vol':>12} {'pre rango':>10} {'reg rango':>10} {'días':>5}")
    print(f"  {'-'*82}")
    agg_pm, agg_9am, agg_pmrange, agg_regrange, agg_volratio = [], [], [], [], []
    for tk in TOP10:
        try:
            bars = fetch_15m(tk)
        except Exception as e:
            print(f"  {tk:>6}  err {e}"); continue
        byday = {}
        for hm, d, o, h, l, c, v in bars:
            byday.setdefault(d, []).append((hm, o, h, l, c, v))
        pm_n, n9_n, pmr, regr, volr = [], [], [], [], []
        for d, rows in byday.items():
            rows = sorted(rows)
            rmap = {r[0]: r for r in rows}
            if "09:30" not in rmap:
                continue
            open0 = rmap["09:30"][1]
            if open0 <= 0:
                continue
            # último bar pre-market (09:15 ET = 08:15 Lima)
            if "09:15" in rmap:
                pm = rmap["09:15"]
                pm_n.append((pm[4] / open0 - 1) * 100)
                pmr.append((pm[2] - pm[3]) / open0 * 100)          # rango del bar pre-market
                regr.append((rmap["09:30"][2] - rmap["09:30"][3]) / open0 * 100)  # rango bar apertura
                if rmap["09:30"][5] > 0:
                    volr.append(pm[5] / rmap["09:30"][5])          # vol pre vs vol apertura
            # 9am Lima = 10:00 ET -> bar que cierra a las 10:00 = el que abre 09:45
            if "09:45" in rmap:
                n9_n.append((rmap["09:45"][4] / open0 - 1) * 100)
        if not pm_n:
            print(f"  {tk:>6}  sin pre-market"); continue
        pm_m = statistics.mean(pm_n); n9_m = statistics.mean(n9_n) if n9_n else 0
        print(f"  {tk:>6} {pm_m:>+10.3f}% {n9_m:>+8.3f}% {pm_m-n9_m:>+10.3f}% "
              f"{(statistics.mean(volr) if volr else 0):>11.2f}x {statistics.mean(pmr):>+9.3f}% "
              f"{statistics.mean(regr):>+9.3f}% {len(pm_n):>5}")
        agg_pm.append(pm_m); agg_9am.append(n9_m); agg_pmrange.append(statistics.mean(pmr))
        agg_regrange.append(statistics.mean(regr)); agg_volratio.append(statistics.mean(volr) if volr else 0)

    if agg_pm:
        print(f"  {'-'*82}")
        pm = statistics.mean(agg_pm); n9 = statistics.mean(agg_9am)
        print(f"  {'PROM':>6} {pm:>+10.3f}% {n9:>+8.3f}% {pm-n9:>+10.3f}% "
              f"{statistics.mean(agg_volratio):>11.2f}x {statistics.mean(agg_pmrange):>+9.3f}% "
              f"{statistics.mean(agg_regrange):>+9.3f}%")
        print(f"\n  LECTURA:")
        print(f"  - 'pre 08:15L' = precio pre-market (último bar antes del open) vs la apertura.")
        print(f"  - 'pre vs 9am' < 0 → el pre-market está MÁS BAJO que a las 9am Lima (lo que Oscar observó).")
        print(f"  - 'pre/reg vol' = liquidez del pre-market vs apertura. Si <<1 (ej. 0.1x), el pre-market es")
        print(f"    ILÍQUIDO → spread ANCHO → el 'precio más bajo' del chart NO es el que pagás (el ask está")
        print(f"    arriba). El 'pre rango' alto vs 'reg rango' confirma el spread/volatilidad del pre-market.")
        print(f"  - VEREDICTO: pre-market conviene SOLO si (pre vs 9am) negativo SUPERA el spread extra del")
        print(f"    pre-market (proxy: la diferencia de rango). Si no, mejor 8:30-9:00 Lima en sesión regular.")
    print(f"\n{'='*100}\n")


if __name__ == "__main__":
    main()
