#!/usr/bin/env python3
"""Extiende la caché de barras 1m hacia atrás (Alpaca SIP free) para más muestra de momentum.
Sobrescribe data/bars_cache/{T}_1m.parquet con el rango ampliado. Uso: python research/extend_bars_history.py"""
from __future__ import annotations
import os, sys, glob, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests, pandas as pd
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _l in (Path(_ROOT)/".env").read_text().splitlines():
    _l=_l.strip()
    if "=" in _l and not _l.startswith("#"):
        k,v=_l.split("=",1); os.environ.setdefault(k.strip(),v.strip())
CACHE=os.path.join(_ROOT,"data","bars_cache")
START=datetime(2026,2,1,tzinfo=timezone.utc)
END=datetime.now(timezone.utc)-timedelta(minutes=20)
H={"APCA-API-KEY-ID":os.environ["ALPACA_API_KEY"],"APCA-API-SECRET-KEY":os.environ["ALPACA_SECRET_KEY"]}
def fetch(sym):
    params={"timeframe":"1Min","start":START.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":END.strftime("%Y-%m-%dT%H:%M:%SZ"),"feed":"sip","limit":10000,"sort":"asc"}
    rows=[]
    while True:
        r=requests.get(f"https://data.alpaca.markets/v2/stocks/{sym}/bars",headers=H,params=params,timeout=60)
        if r.status_code!=200:
            print(f"  {sym} {r.status_code}"); break
        d=r.json(); rows.extend(d.get("bars") or [])
        tok=d.get("next_page_token")
        if not tok: break
        params["page_token"]=tok
    return rows
tickers=[os.path.basename(f).replace("_1m.parquet","") for f in sorted(glob.glob(os.path.join(CACHE,"*_1m.parquet")))]
print(f"Extendiendo {len(tickers)} tickers a {START.date()}..{END.date()}")
t0=time.time()
for i,tk in enumerate(tickers,1):
    bars=fetch(tk)
    if not bars: continue
    df=pd.DataFrame([(b["t"],b["o"],b["h"],b["l"],b["c"],b["v"]) for b in bars],columns=["t","o","h","l","c","v"])
    df["ts"]=pd.to_datetime(df["t"],utc=True); df=df.drop(columns="t").set_index("ts").sort_index()
    df.to_parquet(os.path.join(CACHE,f"{tk}_1m.parquet"))
    if i%10==0: print(f"  {i}/{len(tickers)} ({time.time()-t0:.0f}s)")
print(f"LISTO en {time.time()-t0:.0f}s")
