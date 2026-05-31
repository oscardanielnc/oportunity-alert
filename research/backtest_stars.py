#!/usr/bin/env python3
"""
SCORE DE ESTRELLAS (1-3⭐) — y validación: ¿predice retorno forward?

Idea HONESTA: las estrellas NO prometen ganancia — miden la FUERZA del setup HOY
(momentum persiste → algo de poder predictivo). Factores (todos sin lookahead):
  - momentum 126d del ticker         (peso 0.5 — el factor validado)
  - calidad de tendencia: price/SMA200 (peso 0.25)
  - momentum del SECTOR (ETF 126d)   (peso 0.25 — "sectores prometedores")
Se rankean cross-sectional entre los candidatos elegibles de cada día (above SMA200 +
breakout 50d). Top tercil = 3⭐, medio = 2⭐, bajo = 1⭐.

VALIDACIÓN: para cada (candidato, día) se mide el retorno forward a 20/40/60 días.
Si 3⭐ > 2⭐ > 1⭐ en retorno medio y hit-rate → el score PREDICE → vale. Si no → decoración.
Muestreo cada 5 días para reducir solapamiento. Sin slippage.
"""
import os, sys, json, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_momentum import fetch_daily, build_calendar

START = "2022-01-01T00:00:00Z"
BREAKOUT, REGIME, MOM = 50, 200, 126
HORIZONS = [20, 40, 60]
SAMPLE_EVERY = 5
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Mapeo ticker -> ETF de sector (para momentum sectorial)
SECTOR_ETF = {}
def _m(tks, etf):
    for t in tks: SECTOR_ETF[t] = etf
_m("NVDA AMD MU INTC AVGO MRVL QCOM TSM AMAT LRCX KLAC ASML TXN ADI SMCI ANET VRT ALAB COHR LITE GLW AAOI STX WDC SNDK".split(), "SMH")
_m("MSFT AAPL GOOGL GOOG AMZN META ORCL CRM NOW INTU SNOW PANW CRWD IBM CSCO PLTR APP SPOT UBER HOOD CRWV NBIS IONQ RGTI NOK".split(), "XLK")
_m("V MA JPM GS BAC".split(), "XLF")
_m("LLY UNH JNJ".split(), "XLV")
_m("CAT GE BA APH RKLB ASTS".split(), "XLI")
_m("WMT COST HD TSLA F".split(), "XLY")
_m("XOM CVX".split(), "XLE")
_m("NEE GEV BE".split(), "XLU")
_m("MSTR COIN IREN CRCL".split(), "XLK")
ETFS = sorted(set(SECTOR_ETF.values()))
DEFAULT_ETF = "XLK"


def pre(bars):
    c = [b["c"] for b in bars]; n = len(bars)
    sma = [None]*n; mom = [None]*n; bkh = [None]*n; cs = 0.0
    for i in range(n):
        cs += c[i]
        if i >= REGIME: cs -= c[i-REGIME]
        if i >= REGIME-1: sma[i] = cs/REGIME
        if i >= BREAKOUT: bkh[i] = max(c[i-BREAKOUT:i])
        if i >= MOM and c[i-MOM] > 0: mom[i] = c[i]/c[i-MOM]-1
    return {"c": c, "sma": sma, "mom": mom, "bkh": bkh}


def pctrank(val, arr):
    if not arr: return 0.5
    return sum(1 for x in arr if x <= val) / len(arr)


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*90}\n  SCORE DE ESTRELLAS — validación predictiva | {len(uni)} tickers | desde 2022\n{'#'*90}")
    raw = {}
    for tk in uni + ETFS:
        b = fetch_daily(tk, START)
        if len(b) > REGIME + MOM: raw[tk] = b
    etf_mom = {}
    for e in ETFS:
        if e in raw:
            etf_mom[e] = {raw[e][i]["t"]: pre(raw[e])["mom"][i] for i in range(len(raw[e]))}
    data = {tk: {"bars": raw[tk], "idx": {b["t"]: i for i, b in enumerate(raw[tk])}, "pre": pre(raw[tk])}
            for tk in uni if tk in raw}
    cal = build_calendar(data)
    print(f"  datos OK: {len(data)} tickers + {len(etf_mom)} ETFs de sector")

    # recolectar observaciones (star, fwd_ret_H) por horizonte
    obs = {H: [] for H in HORIZONS}
    for k in range(REGIME + MOM, len(cal), SAMPLE_EVERY):
        d = cal[k]
        # candidatos elegibles HOY (above SMA200 + breakout)
        elig = []
        for tk, dd in data.items():
            i = dd["idx"].get(d)
            if i is None or i < REGIME + MOM: continue
            p = dd["pre"]; c = p["c"][i]; sma = p["sma"][i]; bkh = p["bkh"][i]; m = p["mom"][i]
            if sma and bkh and m is not None and c > sma and c >= bkh:
                etf = SECTOR_ETF.get(tk, DEFAULT_ETF)
                sm = etf_mom.get(etf, {}).get(d)
                elig.append({"tk": tk, "i": i, "mom": m, "trend": c/sma - 1,
                             "secmom": sm if sm is not None else 0.0})
        if len(elig) < 3: continue
        # ranks cross-sectional
        moms = [e["mom"] for e in elig]; trends = [e["trend"] for e in elig]; secs = [e["secmom"] for e in elig]
        for e in elig:
            comp = 0.5*pctrank(e["mom"], moms) + 0.25*pctrank(e["trend"], trends) + 0.25*pctrank(e["secmom"], secs)
            e["comp"] = comp
        elig.sort(key=lambda e: -e["comp"])
        n = len(elig); t1 = n//3; t2 = 2*n//3
        for rank_pos, e in enumerate(elig):
            star = 3 if rank_pos < t1 else (2 if rank_pos < t2 else 1)
            dd = data[e["tk"]]; i = e["i"]; c0 = dd["pre"]["c"][i]
            for H in HORIZONS:
                if i + H < len(dd["bars"]):
                    fwd = dd["bars"][i+H]["c"]/c0 - 1
                    obs[H].append((star, fwd*100))

    # reporte por estrella
    for H in HORIZONS:
        print(f"\n{'='*90}\n  RETORNO FORWARD {H} días por ESTRELLA  (n={len(obs[H])} observaciones)\n{'='*90}")
        print(f"  {'Estrellas':<10} {'n':>6} {'Ret medio':>11} {'Mediana':>9} {'Hit-rate':>9} {'Ret>+10%':>9}")
        by = defaultdict(list)
        for star, fwd in obs[H]: by[star].append(fwd)
        for star in (3, 2, 1):
            v = by[star]
            if not v: continue
            hr = sum(1 for x in v if x > 0)/len(v)*100
            big = sum(1 for x in v if x > 10)/len(v)*100
            print(f"  {'⭐'*star:<10} {len(v):>6} {statistics.mean(v):>+10.2f}% {statistics.median(v):>+8.2f}% "
                  f"{hr:>8.0f}% {big:>8.0f}%")
    print(f"\n  Lectura: si 3⭐ > 2⭐ > 1⭐ MONÓTONO en retorno medio Y hit-rate → el score PREDICE.")
    print(f"  Si están mezclados → es decoración y NO se pone (no doy estrellas que mienten).\n")


if __name__ == "__main__":
    main()
