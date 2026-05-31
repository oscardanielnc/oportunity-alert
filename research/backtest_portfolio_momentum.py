import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
BACKTEST DE PORTAFOLIO — Momentum diario LONG con ranking de fuerza relativa.

Modelo:
  Universo de lideres. Cada dia:
    - Regimen   : solo candidatos con close > SMA200
    - Entrada   : breakout (nuevo maximo de cierre de N dias)
    - Ranking   : entre candidatos, prioriza los de MAYOR momentum (retorno 126d)
    - Top-K     : mantiene a lo sumo K posiciones simultaneas (concentracion)
    - Salida    : trailing chandelier = (max high desde entrada) - mult*ATR
    - Ejecucion : al OPEN del dia siguiente (sin lookahead)
    - Comision  : $1 abrir + $1 cerrar (LONG). Sin shorts.
    - Sizing    : equity/K por posicion, sin apalancamiento (cash limita)

Reporta metricas MENSUALES: retorno medio/mediano, mejor/peor mes, % meses
positivos, volatilidad, CAGR, max drawdown, Sharpe. Benchmark: QQQ buy&hold.
"""
import os, sys, statistics
from datetime import datetime, timezone
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests

ALPACA_BASE = "https://data.alpaca.markets"
START = "2022-01-01T00:00:00Z"
START_CAPITAL = 10000.0
FEE_USD = 1.0           # por lado (abrir y cerrar) → $2 round-trip
MOM_LOOKBACK = 126      # ~6 meses para ranking de fuerza relativa

UNIVERSE = ["NVDA","AMD","MU","PLTR","IONQ","QBTS","APP","RDDT","AVGO","MSFT",
            "DELL","SMCI","TSLA","META","GOOGL","AMZN","AAPL","NFLX","COIN","MRVL","ANET","CRWD"]


def hdrs():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def fetch_daily(ticker, start=START):
    bars, params = [], {"timeframe":"1Day","start":start,"feed":"sip","limit":1000,
                        "sort":"asc","adjustment":"all"}
    try:
        while True:
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=hdrs(), timeout=30)
            r.raise_for_status()
            d = r.json(); chunk = d.get("bars") or []
            bars.extend(chunk)
            if not d.get("next_page_token"): break
            params["page_token"] = d["next_page_token"]
    except Exception as e:
        print(f"  fetch err {ticker}: {e}")
    return [{"t":b["t"][:10],"o":float(b["o"]),"h":float(b["h"]),
             "l":float(b["l"]),"c":float(b["c"])} for b in bars]


def precompute(bars, breakout, atr_n=22, regime=200):
    """Devuelve arrays alineados a bars: sma, atr, prior breakout high, momentum."""
    n = len(bars); closes=[b["c"] for b in bars]
    sma=[None]*n; atrr=[None]*n; bkh=[None]*n; mom=[None]*n
    csum=0.0
    for i in range(n):
        csum+=closes[i]
        if i>=regime: csum-=closes[i-regime]
        if i>=regime-1: sma[i]=csum/regime
        if i>=atr_n:
            trs=[max(bars[j]["h"]-bars[j]["l"],abs(bars[j]["h"]-bars[j-1]["c"]),
                     abs(bars[j]["l"]-bars[j-1]["c"])) for j in range(i-atr_n+1,i+1)]
            atrr[i]=sum(trs)/atr_n
        if i>=breakout: bkh[i]=max(closes[i-breakout:i])
        if i>=MOM_LOOKBACK and closes[i-MOM_LOOKBACK]>0:
            mom[i]=closes[i]/closes[i-MOM_LOOKBACK]-1
    return {"sma":sma,"atr":atrr,"bkh":bkh,"mom":mom}


def run_portfolio(data, calendar, K, breakout, mult, regime=200,
                  index_data=None, vol_target=None):
    """data[tk] = {bars, idx(date->i), pre}. calendar = master date list.

    index_data: si se pasa (dict con bars/idx/sma200), solo abre NUEVAS posiciones
                cuando el indice (QQQ) esta sobre su SMA200 (filtro de regimen macro).
    vol_target: si se pasa (ej 0.03), reduce el tamano de posiciones con ATR% alto
                (volatility targeting, sin apalancar — solo achica los volatiles).
    """
    cash = START_CAPITAL
    positions = {}   # tk -> {shares, entry, hh}
    daily_eq = []    # (date, equity)
    trades = []

    warm = max(regime, MOM_LOOKBACK, breakout)+1

    for d in calendar:
        # ---------- 1) salidas ----------
        to_exit=[]
        for tk,p in positions.items():
            dd=data[tk]; i=dd["idx"].get(d)
            if i is None: continue
            b=dd["bars"][i]
            p["hh"]=max(p["hh"],b["h"])
            a=dd["pre"]["atr"][i]
            if a is None: continue
            stop=p["hh"]-mult*a
            if b["c"]<stop and i+1<len(dd["bars"]):
                to_exit.append((tk,i))
        for tk,i in to_exit:
            dd=data[tk]; xo=dd["bars"][i+1]["o"]; p=positions.pop(tk)
            cash += p["shares"]*xo - FEE_USD
            gross=(xo/p["entry"]-1)*100
            trades.append({"tk":tk,"gross":gross,"out":dd["bars"][i+1]["t"]})

        # ---------- 2) entradas ----------
        # Filtro de regimen macro: no abrir nuevas si el indice esta bajo su SMA200
        regime_ok=True
        if index_data is not None:
            ii=index_data["idx"].get(d)
            if ii is not None and index_data["sma"][ii] is not None:
                regime_ok = index_data["bars"][ii]["c"] > index_data["sma"][ii]
        free=K-len(positions)
        if free>0 and regime_ok:
            cands=[]
            for tk in data:
                if tk in positions: continue
                dd=data[tk]; i=dd["idx"].get(d)
                if i is None or i<warm or i+1>=len(dd["bars"]): continue
                pre=dd["pre"]; c=dd["bars"][i]["c"]
                sma=pre["sma"][i]; bkh=pre["bkh"][i]; m=pre["mom"][i]
                if sma and bkh and m is not None and c>sma and c>=bkh:
                    cands.append((m,tk,i))
            cands.sort(reverse=True)            # mayor momentum primero
            equity_now=cash+sum(positions[t]["shares"]*_close_on(data[t],d) for t in positions)
            for m,tk,i in cands[:free]:
                dd=data[tk]; eo=dd["bars"][i+1]["o"]
                base=equity_now/K
                if vol_target is not None:
                    a=dd["pre"]["atr"][i]; atr_pct=(a/dd["bars"][i]["c"]) if a else vol_target
                    base*=min(1.0, vol_target/atr_pct) if atr_pct>0 else 1.0
                target=min(cash, base)
                if target<=FEE_USD+1: continue
                shares=(target-FEE_USD)/eo
                cash-=shares*eo+FEE_USD
                positions[tk]={"shares":shares,"entry":eo,"hh":dd["bars"][i+1]["h"]}

        # ---------- 3) marcar equity ----------
        eq=cash+sum(positions[tk]["shares"]*_close_on(data[tk],d) for tk in positions)
        daily_eq.append((d,eq))

    return daily_eq, trades


_last_close_cache={}
def _close_on(dd, d):
    i=dd["idx"].get(d)
    if i is not None: return dd["bars"][i]["c"]
    # ultimo cierre conocido <= d
    bars=dd["bars"]
    lo,hi=0,len(bars)-1; ans=bars[0]["c"]
    for b in bars:
        if b["t"]<=d: ans=b["c"]
        else: break
    return ans


def monthly_stats(daily_eq):
    by_month={}
    for d,eq in daily_eq:
        by_month[d[:7]]=eq           # ultimo del mes queda
    months=sorted(by_month)
    rets=[]
    prev=START_CAPITAL
    for m in months:
        rets.append(by_month[m]/prev-1)
        prev=by_month[m]
    rets=[r*100 for r in rets]
    # max drawdown sobre equity diaria
    peak=-1e9; mdd=0.0
    for _,eq in daily_eq:
        peak=max(peak,eq); mdd=min(mdd,eq/peak-1)
    yrs=len(daily_eq)/252
    final=daily_eq[-1][1]
    cagr=(final/START_CAPITAL)**(1/yrs)-1 if yrs>0 else 0
    pos=[r for r in rets if r>0]
    mean=statistics.mean(rets) if rets else 0
    std=statistics.pstdev(rets) if len(rets)>1 else 0
    sharpe=(mean/std*(12**0.5)) if std>0 else 0
    return {
        "months":len(rets),"total":round((final/START_CAPITAL-1)*100,0),
        "cagr":round(cagr*100,1),"mean_m":round(mean,2),"med_m":round(statistics.median(rets),2),
        "best_m":round(max(rets),1),"worst_m":round(min(rets),1),
        "pos_pct":round(len(pos)/len(rets)*100) if rets else 0,
        "vol_m":round(std,2),"mdd":round(mdd*100,1),"sharpe":round(sharpe,2),
    }


def build_calendar(data):
    dates=set()
    for tk in data: dates.update(data[tk]["idx"].keys())
    return sorted(dates)


def main():
    print(f"\n{'='*108}")
    print(f"  PORTAFOLIO MOMENTUM — ranking fuerza relativa + top-K + chandelier   "
          f"(capital ${START_CAPITAL:.0f}, fee $1/lado)")
    print(f"{'='*108}")

    # fetch
    raw={}
    for tk in UNIVERSE+["QQQ"]:
        b=fetch_daily(tk)
        if len(b)>260: raw[tk]=b
    qqq=raw.pop("QQQ",None)

    def build(breakout):
        data={}
        for tk,bars in raw.items():
            data[tk]={"bars":bars,"idx":{b["t"]:i for i,b in enumerate(bars)},
                      "pre":precompute(bars,breakout)}
        return data

    # ---- GRID de configuraciones (palancas) ----
    configs=[]
    for K in (3,5,8):
        for breakout in (20,50):
            for mult in (3.0,4.0):
                configs.append((K,breakout,mult))

    print(f"\n  {'K':>2} {'brk':>4} {'mult':>5} {'Trades':>6} {'Total%':>8} {'CAGR%':>6} "
          f"{'Mes med%':>8} {'Mes prom%':>9} {'Mejor':>6} {'PeorMes':>8} {'%+meses':>7} {'VolMes':>7} {'MaxDD%':>7} {'Sharpe':>6}")
    print(f"  {'-'*104}")
    results={}
    data_cache={}
    for (K,breakout,mult) in configs:
        if breakout not in data_cache: data_cache[breakout]=build(breakout)
        data=data_cache[breakout]
        cal=build_calendar(data)
        deq,trades=run_portfolio(data,cal,K,breakout,mult)
        s=monthly_stats(deq)
        results[(K,breakout,mult)]=s
        print(f"  {K:>2} {breakout:>4} {mult:>5.1f} {len(trades):>6} {s['total']:>+7.0f}% {s['cagr']:>+5.1f}% "
              f"{s['med_m']:>+7.2f}% {s['mean_m']:>+8.2f}% {s['best_m']:>+5.0f}% {s['worst_m']:>+7.1f}% "
              f"{s['pos_pct']:>6}% {s['vol_m']:>+6.2f}% {s['mdd']:>+6.1f}% {s['sharpe']:>6.2f}")

    # benchmark QQQ buy&hold
    if qqq:
        deq=[(b["t"],START_CAPITAL*b["c"]/qqq[0]["c"]) for b in qqq]
        s=monthly_stats(deq)
        print(f"  {'-'*104}")
        print(f"  QQQ buy&hold (benchmark): Total {s['total']:+.0f}% | CAGR {s['cagr']:+.1f}% | "
              f"Mes prom {s['mean_m']:+.2f}% | PeorMes {s['worst_m']:+.1f}% | "
              f"%+meses {s['pos_pct']}% | MaxDD {s['mdd']:+.1f}% | Sharpe {s['sharpe']:.2f}")

    # ---- PALANCAS DE MEJORA sobre configs candidatas ----
    idx_obj=None
    if qqq:
        idx_obj={"bars":qqq,"idx":{b["t"]:i for i,b in enumerate(qqq)},
                 "sma":precompute(qqq,50)["sma"]}
    print(f"\n  {'-'*104}")
    print(f"  PALANCAS DE MEJORA (sobre configs candidatas) — base vs +filtro macro QQQ vs +sizing por volatilidad")
    print(f"  {'Config':<22} {'Total%':>8} {'CAGR%':>6} {'Mes prom%':>9} {'PeorMes':>8} {'MaxDD%':>7} {'Sharpe':>6} {'Trades':>6}")
    print(f"  {'-'*100}")
    for (K,breakout,mult) in [(5,50,4.0),(8,50,4.0),(3,50,4.0)]:
        data=data_cache[breakout]; cal=build_calendar(data)
        variants=[("base",dict()),
                  ("+macro QQQ",dict(index_data=idx_obj)),
                  ("+vol sizing",dict(vol_target=0.03)),
                  ("+macro +vol",dict(index_data=idx_obj,vol_target=0.03))]
        for label,kw in variants:
            deq,trades=run_portfolio(data,cal,K,breakout,mult,**kw)
            s=monthly_stats(deq)
            name=f"K{K} brk{breakout} m{mult} {label}"
            print(f"  {name:<22} {s['total']:>+7.0f}% {s['cagr']:>+5.1f}% {s['mean_m']:>+8.2f}% "
                  f"{s['worst_m']:>+7.1f}% {s['mdd']:>+6.1f}% {s['sharpe']:>6.2f} {len(trades):>6}")
        print()

    print(f"\n{'='*108}")
    print("  Mes prom% = retorno mensual promedio. PeorMes = peor mes individual. "
          "MaxDD = caida maxima pico-valle (riesgo real).")
    print("  Sharpe mensual anualizado. Sin slippage. Lectura: buscar mejor Sharpe / menor MaxDD, no solo retorno.")
    print(f"{'='*108}\n")


if __name__ == "__main__":
    main()
