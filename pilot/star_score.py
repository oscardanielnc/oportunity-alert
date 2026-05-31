#!/usr/bin/env python3
"""
Star score — flag BINARIO ⭐ TOP de mayor convicción (validado).

Validado (research/backtest_stars.py): el TOP tercil del score predice retorno forward
MUCHO mayor (20d +10.9% vs ~3.2%; 60d +24.6% vs ~9%; hit-rate 64-67% vs 55-61%). El
medio/bajo NO se distinguen entre sí → por eso es BINARIO: TOP vs estándar, no 3 niveles.

Score = momentum 126d (0.5) + calidad de tendencia price/SMA200 (0.25) + momentum del
SECTOR (ETF 126d, 0.25). Rankeado cross-sectional entre los candidatos; top tercil = ⭐ TOP.

Uso: en run_pilot, marcar qué señales/posiciones de Marea son ⭐ TOP, + fuerza por sector.
"""

# Mapeo ticker -> ETF de sector (para momentum sectorial)
SECTOR_ETF = {}
def _m(tks, etf):
    for t in tks.split():
        SECTOR_ETF[t] = etf
_m("NVDA AMD MU INTC AVGO MRVL QCOM TSM AMAT LRCX KLAC ASML TXN ADI SMCI ANET VRT ALAB COHR LITE GLW AAOI STX WDC SNDK", "SMH")
_m("MSFT AAPL GOOGL GOOG AMZN META ORCL CRM NOW INTU SNOW PANW CRWD IBM CSCO PLTR APP SPOT UBER HOOD CRWV NBIS IONQ RGTI NOK", "XLK")
_m("V MA JPM GS BAC", "XLF")
_m("LLY UNH JNJ", "XLV")
_m("CAT GE BA APH RKLB ASTS", "XLI")
_m("WMT COST HD TSLA F", "XLY")
_m("XOM CVX", "XLE")
_m("NEE GEV BE", "XLU")
_m("MSTR COIN IREN CRCL", "XLK")
SECTOR_ETFS = sorted(set(SECTOR_ETF.values()))
DEFAULT_ETF = "XLK"
SECTOR_NAME = {"SMH": "Semis/IA", "XLK": "Software/Tech", "XLF": "Financieras",
               "XLV": "Salud", "XLI": "Industriales/Defensa", "XLY": "Consumo",
               "XLE": "Energía", "XLU": "Power/Utilities"}


def _pctrank(val, arr):
    if not arr:
        return 0.5
    return sum(1 for x in arr if x <= val) / len(arr)


def compute_top(rows, sector_mom):
    """
    rows: lista de {tk, mom, trend}. sector_mom: {etf: momentum}.
    Devuelve (set_TOP, {tk: score}). TOP = tercil superior del score compuesto.
    """
    rows = [r for r in rows if r.get("mom") is not None]
    if not rows:
        return set(), {}
    for r in rows:
        r["sm"] = sector_mom.get(SECTOR_ETF.get(r["tk"], DEFAULT_ETF), 0.0)
    moms = [r["mom"] for r in rows]; trends = [r["trend"] for r in rows]; secs = [r["sm"] for r in rows]
    for r in rows:
        r["score"] = round(0.5*_pctrank(r["mom"], moms) + 0.25*_pctrank(r["trend"], trends)
                           + 0.25*_pctrank(r["sm"], secs), 3)
    rows.sort(key=lambda r: -r["score"])
    ntop = max(1, len(rows)//3)
    top = {r["tk"] for r in rows[:ntop]}
    return top, {r["tk"]: r["score"] for r in rows}


def sector_strength(sector_mom):
    """Ranking de sectores por momentum del ETF (los 'sectores prometedores' del momento)."""
    out = [{"etf": e, "name": SECTOR_NAME.get(e, e), "mom_pct": round(m*100, 1)}
           for e, m in sector_mom.items() if m is not None]
    out.sort(key=lambda x: -x["mom_pct"])
    return out
