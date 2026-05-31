#!/usr/bin/env python3
"""
Universo por reglas (liquidez) — sin elegir ganadores a mano.

Reglas:
  1. Assets tradables us_equity en NASDAQ o NYSE (excluye ARCA/BATS = casi todo ETF).
  2. Excluir ETFs/ETNs/fondos/preferentes/warrants por heuristica de nombre y simbolo.
  3. Rankear por dollar-volume promedio (precio x volumen) de ~10 dias.
  4. Filtrar precio >= MIN_PRICE y dollar-vol >= MIN_DOLLAR_VOL.
  5. Tomar TOP_N por liquidez.

Cachea a data/pilot_universe.json con timestamp. Refresco recomendado: semanal.
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CACHE_PATH = os.path.join(DATA_DIR, "pilot_universe.json")

MIN_PRICE       = 10.0
MIN_DOLLAR_VOL  = 50e6      # $50M/dia promedio
TOP_N           = 80
LOOKBACK_DAYS   = 16
BATCH           = 200
REFRESH_DAYS    = 7

_ETF_TOKENS = ("ETF","ETN","FUND"," TRUST","SPDR","ISHARES","PROSHARES","DIREXION",
               "INVESCO","VANGUARD","INDEX","NOTES"," 2X"," 3X","BULL ","BEAR ",
               "PREFERRED","DEPOSITARY","WARRANT","ACQUISITION CORP")


def _hdrs():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def _list_candidates():
    """Common stocks tradables en NASDAQ/NYSE, filtrando ETFs/derivados por nombre/simbolo."""
    r = requests.get("https://paper-api.alpaca.markets/v2/assets",
                     params={"status":"active","asset_class":"us_equity"},
                     headers=_hdrs(), timeout=40)
    r.raise_for_status()
    out = []
    for a in r.json():
        if not a.get("tradable"):                       continue
        if a.get("exchange") not in ("NASDAQ","NYSE"):  continue
        sym = a.get("symbol","")
        if not sym.isalpha() or len(sym) > 5:           continue   # excluye warrants/units/preferentes
        name = (a.get("name") or "").upper()
        if any(tok in name for tok in _ETF_TOKENS):     continue
        out.append(sym)
    return sorted(set(out))


def _avg_dollar_vol(symbols):
    """Devuelve {sym: (avg_dollar_vol, last_close)} via barras diarias multi-simbolo."""
    start = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i+BATCH]
        try:
            r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                             params={"symbols":",".join(chunk),"timeframe":"1Day",
                                     "start":start,"feed":"sip","limit":10000},
                             headers=_hdrs(), timeout=40)
            if r.status_code != 200:
                continue
            bars = r.json().get("bars", {})
            for sym, bb in bars.items():
                if not bb: continue
                dv = sum(x["c"]*x["v"] for x in bb) / len(bb)
                res[sym] = (dv, bb[-1]["c"])
        except Exception:
            pass
        time.sleep(0.1)
    return res


def build_universe(top_n=TOP_N, save=True, verbose=True):
    cands = _list_candidates()
    if verbose: print(f"[universe] candidatos NASDAQ/NYSE (stocks): {len(cands)}")
    dv = _avg_dollar_vol(cands)
    if verbose: print(f"[universe] con datos de liquidez: {len(dv)}")
    rows = [(sym, d, c) for sym,(d,c) in dv.items()
            if c >= MIN_PRICE and d >= MIN_DOLLAR_VOL]
    rows.sort(key=lambda x: -x[1])
    top = rows[:top_n]
    tickers = [s for s,_,_ in top]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(tickers),
        "params": {"min_price":MIN_PRICE,"min_dollar_vol":MIN_DOLLAR_VOL,"top_n":top_n},
        "tickers": tickers,
        "detail": [{"sym":s,"dollar_vol_M":round(d/1e6),"price":round(c,2)} for s,d,c in top],
    }
    if save:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(payload, f, indent=2)
    if verbose:
        print(f"[universe] universo final: {len(tickers)} tickers (guardado en {CACHE_PATH})")
        print("  top 15:", ", ".join(f"{s}(${d/1e9:.1f}B)" for s,d,_ in top[:15]))
    return payload


def load_universe(auto_refresh=True):
    """Carga el universo cacheado; reconstruye si no existe o esta viejo (>REFRESH_DAYS)."""
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                p = json.load(f)
            gen = datetime.fromisoformat(p["generated_at"])
            age_days = (datetime.now(timezone.utc) - gen).total_seconds() / 86400
            if not auto_refresh or age_days < REFRESH_DAYS:
                return p["tickers"]
        except Exception:
            pass
    return build_universe(verbose=False)["tickers"]


if __name__ == "__main__":
    build_universe()
