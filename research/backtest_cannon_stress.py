#!/usr/bin/env python3
"""
STRESS TEST — "Cañonazo bajista" (short intradia tras miss severo de earnings).

Regla base validada: Day-1 reaccion <= -8% -> SHORT open, cubrir close (mismo dia).
Aqui la ESTRESAMOS y clasificamos donde funciona mejor: slippage, tamaño, sector,
regimen macro (QQQ vs SMA200), volatilidad, severidad del miss. Objetivo: el subconjunto
de MAXIMA confiabilidad (win seguro), estable por anio. Si pasa con excelencia -> 3a estrategia.

Costos: 0.3%/lado (0.6% RT) + slippage haircut configurable. INTRADIA (sin carry/overnight).
Reusa fetch_all (Alpaca) + fetch_earnings_events (yfinance). ASCII only.
Uso: python -m research.backtest_cannon_stress [--fresh]
"""
import os, sys, time, json, statistics as st
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from super_backtest import fetch_all
from backtest_ped import fetch_earnings_events

WEEKS = 260            # ~5 anios
COMM_RT = 0.6
SLIP = 0.3             # haircut de slippage por trade (realista en open/close liquido)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cannon_stress_cache.json")

# Universo ampliado: large/mega (PED) + high-beta/small (conviction_gates sectors) para segmentar tamaño
UNIVERSE = sorted(set([
    "NVDA","AMD","MU","AVGO","MSFT","GOOG","META","AMZN","AAPL","TSM","ORCL","CRM","ADBE",
    "NOW","PANW","CRWD","SNOW","NFLX","QCOM","INTC","MRVL","DELL","ANET","SMCI","ARM","UBER",
    "SHOP","PLTR","COIN","ABNB","MELI","RDDT","APP","COHR","ASML","LRCX","KLAC","AMAT","TXN","MDB",
    # high-beta / small
    "QBTS","IONQ","RGTI","QUBT","SOUN","BBAI","APLD","IREN","RKLB","LUNR","JOBY","ACHR","ASTS","BE",
]))

SEMIS = {"NVDA","AMD","AVGO","MU","MRVL","QCOM","INTC","TSM","ASML","LRCX","KLAC","AMAT","TXN","COHR","SMCI","ARM"}
SOFTWARE = {"MSFT","ORCL","CRM","ADBE","NOW","PANW","CRWD","SNOW","PLTR","MDB","APP"}
INTERNET = {"GOOG","META","AMZN","NFLX","UBER","ABNB","SHOP","RDDT","MELI","COIN"}
SPEC = {"QBTS","IONQ","RGTI","QUBT","SOUN","BBAI","APLD","IREN","RKLB","LUNR","JOBY","ACHR","ASTS","BE"}
def sector(tk):
    return "semis" if tk in SEMIS else "software" if tk in SOFTWARE else \
           "internet" if tk in INTERNET else "spec" if tk in SPEC else "otro"

MEGA = {"AAPL","MSFT","GOOG","META","AMZN","NVDA","AVGO","TSM","ORCL"}
LARGE = {"AMD","CRM","ADBE","NOW","NFLX","QCOM","TXN","PANW","UBER","SHOP","ARM","MU","INTC",
         "AMAT","LRCX","KLAC","ASML","PLTR","ANET","DELL","CRWD","COIN","ABNB","MELI","MRVL"}
def size_tier(tk):
    return "mega" if tk in MEGA else "large" if tk in LARGE else "small"


def _rsi(closes, n=14):
    if len(closes) < n+1: return 50.0
    d = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    g=[max(x,0) for x in d]; l=[abs(min(x,0)) for x in d]
    ag=sum(g[:n])/n; al=sum(l[:n])/n
    for i in range(n,len(g)): ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+l[i])/n
    return 100.0 if al==0 else 100-100/(1+ag/al)

def _atr_pct(bars, i, n=14):
    if i < n+1: return 0.0
    trs=[]
    for k in range(i-n,i):
        tr=max(bars[k]["h"]-bars[k]["l"], abs(bars[k]["h"]-bars[k-1]["c"]), abs(bars[k]["l"]-bars[k-1]["c"]))
        trs.append(tr)
    return sum(trs)/n / bars[i-1]["c"] * 100


def build(ticker, bars, events, qqq_regime):
    dates=[b["t"][:10] for b in bars]; idx={d:i for i,d in enumerate(dates)}
    out=[]
    for ev in events:
        E,h=ev["date"],ev["hour"]
        if E not in idx: continue
        ei=idx[E]; r=ei if h=="bmo" else ei+1
        if r-1<55 or r+1>=len(bars): continue
        pre=bars[r-1]["c"]; o=bars[r]["o"]
        if pre<=0 or o<=0: continue
        day1=(bars[r]["c"]-pre)/pre*100
        if day1 > -3: continue                  # solo MISS (reaccion negativa)
        closes=[b["c"] for b in bars[:r]]
        out.append({
            "ticker":ticker,"date":E,"day1_ret":round(day1,2),
            "gap":round((o-pre)/pre*100,2),
            "react_oc":round((bars[r]["c"]-o)/o*100,2),
            "rsi_pre":round(_rsi(closes[-30:]),1),
            "ext_sma50":round((pre-sum(closes[-50:])/50)/(sum(closes[-50:])/50)*100,1),
            "atr_pct":round(_atr_pct(bars,r),1),
            "sector":sector(ticker),"size":size_tier(ticker),
            "regime":qqq_regime.get(E,"?"),
        })
    return out


def load_or_build():
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        return json.load(open(CACHE,encoding="utf-8"))
    now=datetime.now(timezone.utc)
    start=(now-timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd=(now-timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    # regimen QQQ
    q=fetch_all("QQQ","1Day",(now-timedelta(weeks=WEEKS+40)).strftime("%Y-%m-%dT%H:%M:%SZ"),limit=2000)
    qc=[b["c"] for b in q]; qd=[b["t"][:10] for b in q]; reg={}
    for i in range(len(qc)):
        if i>=200: reg[qd[i]]="bull" if qc[i]>sum(qc[i-200:i])/200 else "bear"
    allev=[]
    for tk in UNIVERSE:
        bars=fetch_all(tk,"1Day",start,limit=2000)
        if len(bars)<60: print(f"  {tk:<6} sin datos"); continue
        evs=fetch_earnings_events(tk,sd,sd); time.sleep(0.15)
        allev+=build(tk,bars,evs,reg)
        print(f"  {tk:<6} {len(evs)} earnings")
    json.dump(allev,open(CACHE,"w",encoding="utf-8"))
    return allev


def pnl(e): return -e["react_oc"] - COMM_RT - SLIP   # SHORT neto con slippage

def stats(sub):
    if len(sub)<1: return None
    p=[pnl(e) for e in sub]; w=sum(1 for x in p if x>0)
    worst10=sorted(p)[:max(1,len(p)//10)]
    return dict(n=len(p),win=round(100*w/len(p),1),mean=round(st.mean(p),2),
                med=round(st.median(p),2),worst=round(min(p),1),w10=round(st.mean(worst10),2))

def stable_years(sub):
    by=defaultdict(list)
    for e in sub: by[e["date"][:4]].append(pnl(e))
    yrs=[(y,sum(1 for x in v if x>0)/len(v)*100, st.mean(v)) for y,v in by.items() if len(v)>=4]
    pos=all(m>0 for _,_,m in yrs)
    return yrs, pos

def line(label, sub, minn=10):
    s=stats(sub)
    if not s or s["n"]<minn: print(f"  {label:<34} n={s['n'] if s else 0:<3} (insuf.)"); return
    _,pos=stable_years(sub)
    print(f"  {label:<34} n={s['n']:<3} win={s['win']:>5}% mean{s['mean']:>+6.2f} med{s['med']:>+6.2f} "
          f"worst{s['worst']:>+6.1f} w10{s['w10']:>+6.1f} {'ESTABLE' if pos else 'inestable'}")


def run():
    global SLIP
    print(f"\n{'#'*100}\n  STRESS — CAÑONAZO BAJISTA (short intradia post-miss) | slippage {SLIP}% | {WEEKS} sem\n{'#'*100}")
    ev=load_or_build()
    ev=sorted(ev,key=lambda e:e["date"])
    base=[e for e in ev if e["day1_ret"]<=-8]
    print(f"  eventos miss<=-3%: {len(ev)} | miss<=-8% (base): {len(base)}")

    print(f"\n== 1) SLIPPAGE SENSITIVITY (miss<=-8%) ==")
    for slp in (0.0,0.3,0.5):
        SLIP=slp; s=stats(base)
        print(f"  slippage {slp}% -> win={s['win']}% mean{s['mean']:+.2f}% med{s['med']:+.2f}%")
    SLIP=0.3

    print(f"\n== 2) SEVERIDAD DEL MISS (slip 0.3%) ==")
    for thr in (3,5,8,10,12):
        line(f"miss <= -{thr}%", [e for e in ev if e['day1_ret']<=-thr])

    print(f"\n== 3) POR TAMAÑO (miss<=-8%) ==")
    for sz in ("mega","large","small"): line(f"size={sz}", [e for e in base if e['size']==sz])

    print(f"\n== 4) POR SECTOR (miss<=-8%) ==")
    for sc in ("semis","software","internet","spec","otro"): line(f"sector={sc}", [e for e in base if e['sector']==sc])

    print(f"\n== 5) POR REGIMEN MACRO (miss<=-8%) ==")
    for rg in ("bull","bear"): line(f"regime={rg}", [e for e in base if e['regime']==rg])

    print(f"\n== 6) POR VOLATILIDAD (ATR%, miss<=-8%) ==")
    line("ATR% alto (>5)", [e for e in base if e['atr_pct']>5])
    line("ATR% bajo (<=5)", [e for e in base if e['atr_pct']<=5])

    print(f"\n== 7) EXTENSION/RSI PRE-EARNINGS (miss<=-8%) ==")
    line("sobre-extendido RSI>=60", [e for e in base if e['rsi_pre']>=60])
    line("ext SMA50 >=15%", [e for e in base if e['ext_sma50']>=15])

    print(f"\n== 8) COMBINACIONES (buscando win seguro) ==")
    line("miss<=-8 & NO mega", [e for e in base if e['size']!="mega"])
    line("miss<=-8 & semis/sw/spec", [e for e in base if e['sector'] in ("semis","software","spec")])
    line("miss<=-8 & NO mega & NO internet", [e for e in base if e['size']!="mega" and e['sector']!="internet"])
    line("miss<=-10 & NO mega", [e for e in ev if e['day1_ret']<=-10 and e['size']!="mega"])
    line("miss<=-8 & NO mega & bear", [e for e in base if e['size']!="mega" and e['regime']=="bear"])
    line("miss<=-8 & NO mega & bull", [e for e in base if e['size']!="mega" and e['regime']=="bull"])

    # mejor combo: imprime years
    best=[e for e in base if e['size']!="mega" and e['sector']!="internet"]
    if len(best)>=10:
        yrs,_=stable_years(best)
        print(f"\n  Desglose anual del mejor combo (miss<=-8 & NO mega & NO internet):")
        for y,w,m in sorted(yrs): print(f"    {y}: win={w:>5.0f}% mean{m:>+6.2f}%")
    print()


if __name__ == "__main__":
    run()
