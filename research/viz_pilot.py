import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""Genera graficos explicativos del modelo de momentum para el piloto."""
import os, sys, statistics
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from backtest_portfolio_momentum import (
    fetch_daily, precompute, run_portfolio, build_calendar,
    START_CAPITAL, MOM_LOOKBACK, UNIVERSE, _close_on,
)

OUT = os.path.dirname(os.path.abspath(__file__))
BREAKOUT, ATR_N, REGIME, MULT = 50, 22, 200, 4.0


def single_sim(bars):
    closes=[b["c"] for b in bars]; n=len(bars)
    pre=precompute(bars,BREAKOUT,ATR_N,REGIME)
    sma,atr,bkh=pre["sma"],pre["atr"],pre["bkh"]
    warm=max(REGIME,MOM_LOOKBACK,BREAKOUT)+1
    pos=None; trades=[]; stop_line=[None]*n
    for i in range(warm,n):
        b=bars[i]
        if pos:
            pos["hh"]=max(pos["hh"],b["h"]); a=atr[i]
            if a:
                stop=pos["hh"]-MULT*a; stop_line[i]=stop
                if b["c"]<stop and i+1<n:
                    trades.append({"in":pos["i"],"out":i+1,"in_p":pos["entry"],
                                   "out_p":bars[i+1]["o"],
                                   "gross":(bars[i+1]["o"]/pos["entry"]-1)*100,"open":False})
                    pos=None; continue
        if not pos and i+1<n and sma[i] and bkh[i] and b["c"]>sma[i] and b["c"]>=bkh[i]:
            pos={"i":i+1,"entry":bars[i+1]["o"],"hh":bars[i+1]["h"]}
    if pos:
        trades.append({"in":pos["i"],"out":n-1,"in_p":pos["entry"],
                       "out_p":bars[-1]["c"],"gross":(bars[-1]["c"]/pos["entry"]-1)*100,"open":True})
    return trades, stop_line, pre


def dts(bars): return [datetime.strptime(b["t"],"%Y-%m-%d") for b in bars]


# ============ GRAFICO 1: anatomia de un trade ============
def chart_anatomy():
    tk="MU"
    bars=fetch_daily(tk)
    trades,stop_line,pre=single_sim(bars)
    completed=[t for t in trades if not t["open"] and t["gross"]>20]
    tr=max(completed,key=lambda t:t["gross"]) if completed else max(trades,key=lambda t:t["gross"])
    a,b_=tr["in"],tr["out"]
    lo=max(0,a-45); hi=min(len(bars)-1,b_+12)
    d=dts(bars)[lo:hi+1]
    c=[x["c"] for x in bars[lo:hi+1]]
    sma=[pre["sma"][i] for i in range(lo,hi+1)]
    stp=[stop_line[i] for i in range(lo,hi+1)]

    fig,ax=plt.subplots(figsize=(13,6.5))
    ax.plot(d,c,color="#1f77b4",lw=1.8,label=f"Precio {tk} (cierre)")
    ax.plot(d,sma,color="#888",ls="--",lw=1.3,label="SMA200 (filtro de regimen)")
    sd=[dts(bars)[i] for i in range(lo,hi+1) if stop_line[i] is not None]
    sv=[stop_line[i] for i in range(lo,hi+1) if stop_line[i] is not None]
    ax.step(sd,sv,where="post",color="#d62728",lw=1.6,label="Stop chandelier (4xATR, sube con el precio)")
    ax.scatter([dts(bars)[a]],[tr["in_p"]],marker="^",s=240,color="green",zorder=5,
               label=f"ENTRADA breakout @ ${tr['in_p']:.0f}")
    ax.scatter([dts(bars)[b_]],[tr["out_p"]],marker="v",s=240,color="red",zorder=5,
               label=f"SALIDA stop @ ${tr['out_p']:.0f}  ({tr['gross']:+.0f}%)")
    ax.fill_between(d,sma,c,where=[ (cc or 0)>=(ss or 0) for cc,ss in zip(c,sma)],
                    color="green",alpha=0.04)
    ax.set_title(f"COMO FUNCIONA UN TRADE — {tk}\n"
                 f"1) Precio sobre SMA200 (tendencia)  2) Breakout = entrada  "
                 f"3) Stop sube con el precio  4) Sale cuando el precio lo toca",fontsize=12)
    ax.legend(loc="upper left",fontsize=9.5); ax.grid(alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y")); ax.set_ylabel("Precio ($)")
    fig.tight_layout(); p=os.path.join(OUT,"pilot_anatomy.png"); fig.savefig(p,dpi=110); plt.close(fig)
    print("OK",p); return p


# ============ Portafolio recomendado ============
def run_reco():
    raw={}
    for tk in UNIVERSE+["QQQ"]:
        bb=fetch_daily(tk)
        if len(bb)>260: raw[tk]=bb
    qqq=raw.pop("QQQ")
    data={tk:{"bars":bb,"idx":{x["t"]:i for i,x in enumerate(bb)},
              "pre":precompute(bb,BREAKOUT,ATR_N,REGIME)} for tk,bb in raw.items()}
    idx_obj={"bars":qqq,"idx":{x["t"]:i for i,x in enumerate(qqq)},"sma":precompute(qqq,50)["sma"]}
    cal=build_calendar(data)
    deq,trades=run_portfolio(data,cal,K=5,breakout=BREAKOUT,mult=MULT,
                             index_data=idx_obj,vol_target=0.03)
    return deq,trades,qqq


# ============ GRAFICO 2: equity + drawdown ============
def chart_equity(deq,qqq):
    dd=[datetime.strptime(x[0],"%Y-%m-%d") for x in deq]
    eq=[x[1] for x in deq]
    qmap={b["t"]:b["c"] for b in qqq}; q0=qqq[0]["c"]
    last=q0; qser=[]
    for x in deq:
        if x[0] in qmap: last=qmap[x[0]]
        qser.append(START_CAPITAL*last/q0)
    peak=-1e9; under=[]
    for v in eq:
        peak=max(peak,v); under.append((v/peak-1)*100)

    fig,(ax1,ax2)=plt.subplots(2,1,figsize=(13,8),sharex=True,
                               gridspec_kw={"height_ratios":[3,1]})
    ax1.plot(dd,eq,color="#2ca02c",lw=2,label="Estrategia momentum (K5, +macro, +vol)")
    ax1.plot(dd,qser,color="#888",lw=1.6,ls="--",label="QQQ comprar y aguantar")
    ax1.set_yscale("log"); ax1.set_ylabel("Capital ($, escala log)")
    ax1.legend(loc="upper left",fontsize=10); ax1.grid(alpha=0.25,which="both")
    ax1.set_title(f"CURVA DE CAPITAL — ${START_CAPITAL:.0f} inicial   "
                  f"(final estrategia ${eq[-1]:,.0f}  vs  QQQ ${qser[-1]:,.0f})",fontsize=12)
    ax2.fill_between(dd,under,0,color="#d62728",alpha=0.5)
    ax2.set_ylabel("Drawdown %"); ax2.grid(alpha=0.25)
    ax2.set_title(f"Caidas desde maximos (peor: {min(under):.0f}%)",fontsize=10)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    fig.tight_layout(); p=os.path.join(OUT,"pilot_equity.png"); fig.savefig(p,dpi=110); plt.close(fig)
    print("OK",p); return p


# ============ GRAFICO 3: retornos mensuales ============
def chart_monthly(deq):
    bym={}
    for d,e in deq: bym[d[:7]]=e
    months=sorted(bym); rets=[]; prev=START_CAPITAL
    for m in months: rets.append((bym[m]/prev-1)*100); prev=bym[m]
    xs=[datetime.strptime(m+"-01","%Y-%m-%d") for m in months]
    cols=["#2ca02c" if r>=0 else "#d62728" for r in rets]
    fig,ax=plt.subplots(figsize=(13,5.5))
    ax.bar(xs,rets,width=22,color=cols)
    mean=statistics.mean(rets); med=statistics.median(rets)
    ax.axhline(mean,color="blue",ls=":",lw=1.4,label=f"Promedio {mean:+.1f}%/mes")
    ax.axhline(med,color="orange",ls=":",lw=1.4,label=f"Mediana {med:+.1f}%/mes (mes tipico)")
    worst=min(rets); wi=rets.index(worst)
    ax.annotate(f"peor mes {worst:.0f}%",(xs[wi],worst),textcoords="offset points",
                xytext=(0,-18),ha="center",fontsize=9,color="#d62728")
    ax.set_title("RETORNOS MENSUALES — fijate: la MAYORIA de meses son pequenos,\n"
                 "y unos pocos meses explosivos hacen el grueso de la ganancia (es grumoso)",fontsize=12)
    ax.legend(loc="upper left",fontsize=10); ax.grid(alpha=0.25,axis="y")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y")); ax.set_ylabel("Retorno mensual %")
    fig.tight_layout(); p=os.path.join(OUT,"pilot_monthly.png"); fig.savefig(p,dpi=110); plt.close(fig)
    print("OK",p); return p


if __name__=="__main__":
    chart_anatomy()
    deq,trades,qqq=run_reco()
    chart_equity(deq,qqq)
    chart_monthly(deq)
    print("DONE")
