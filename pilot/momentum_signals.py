#!/usr/bin/env python3
"""
Motor de senales — momentum swing diario (mismas reglas que el backtest validado).

Parametros (config recomendada del backtest de portafolio):
  REGIME=200  : solo LONG si close > SMA200
  BREAKOUT=50 : entrada en nuevo maximo de cierre de 50 dias
  ATR_N=22    : ATR para el stop
  MULT=4.0    : chandelier = max_high_desde_entrada - 4*ATR
  MOM_LOOKBACK=126 : ranking por retorno de ~6 meses (fuerza relativa)
  Macro       : no abrir nuevas si QQQ < su SMA200
"""
import os, sys, time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

REGIME, BREAKOUT, ATR_N, MULT, MOM_LOOKBACK = 200, 50, 22, 4.0, 126
HIST_DAYS = 520
ALPACA_BASE = "https://data.alpaca.markets"


def _hdrs():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def fetch_daily_batch(symbols, lookback_days=HIST_DAYS):
    """Barras diarias ajustadas (split+div) multi-simbolo. -> {sym: [bars]}."""
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    BATCH = 100
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i+BATCH]
        page = None
        while True:
            params = {"symbols":",".join(chunk),"timeframe":"1Day","start":start,
                      "feed":"sip","limit":10000,"adjustment":"all"}
            if page: params["page_token"] = page
            try:
                r = requests.get(f"{ALPACA_BASE}/v2/stocks/bars", params=params,
                                 headers=_hdrs(), timeout=40)
                if r.status_code != 200: break
                j = r.json()
                for sym, bb in (j.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(
                        {"t":b["t"][:10],"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"]}
                        for b in bb)
                page = j.get("next_page_token")
                if not page: break
            except Exception:
                break
        time.sleep(0.1)
    return out


# ── indicadores ──────────────────────────────────────────────────────────────

def _sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _ema(closes, n):
    """EMA simple sembrada con la SMA de las primeras n barras."""
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(closes[:n]) / n
    for c in closes[n:]:
        ema = c * k + ema * (1 - k)
    return ema


def _atr(bars, n=ATR_N):
    if len(bars) < n + 1: return None
    trs = [max(bars[i]["h"]-bars[i]["l"], abs(bars[i]["h"]-bars[i-1]["c"]),
               abs(bars[i]["l"]-bars[i-1]["c"])) for i in range(len(bars)-n, len(bars))]
    return sum(trs) / n


def indicators(bars):
    """Estado tecnico al ultimo cierre. None si faltan datos."""
    if len(bars) < REGIME + 2:
        return None
    closes = [b["c"] for b in bars]
    price  = closes[-1]
    sma200 = _sma(closes, REGIME)
    atr    = _atr(bars)
    prior_high = max(closes[-BREAKOUT-1:-1])             # max cierre de los 50 dias previos (sin hoy)
    mom    = (closes[-1]/closes[-MOM_LOOKBACK-1]-1) if len(closes) > MOM_LOOKBACK else None
    support = min(b["l"] for b in bars[-10:])           # swing low reciente (soporte 10d)
    return {
        "price": price, "sma200": sma200, "atr": atr,
        "prior_high": prior_high, "momentum": mom,
        "ema20": _ema(closes, 20), "support": support,
        "above_sma": price > sma200 if sma200 else False,
        "is_breakout": price >= prior_high,
        "high": bars[-1]["h"], "date": bars[-1]["t"],
    }


def chandelier_stop(highest_high, atr, mult=MULT):
    if atr is None: return None
    return highest_high - mult * atr


def macro_ok(qqq_bars):
    """True si QQQ esta sobre su SMA200 (regimen alcista de mercado)."""
    if not qqq_bars or len(qqq_bars) < REGIME + 1:
        return True                          # sin datos -> no bloquear
    closes = [b["c"] for b in qqq_bars]
    return closes[-1] > _sma(closes, REGIME)


def entry_candidates(indicators_by_ticker):
    """Tickers que cumplen regimen + breakout, ordenados por momentum desc."""
    cands = []
    for tk, ind in indicators_by_ticker.items():
        if ind and ind["above_sma"] and ind["is_breakout"] and ind["momentum"] is not None:
            cands.append((ind["momentum"], tk))
    cands.sort(reverse=True)
    return [tk for _, tk in cands]


def entry_levels(ind):
    """
    Niveles de entrada SUGERIDOS (informativos) para una recomendación de COMPRA.

    NO altera ninguna señal validada: el sistema sigue decidiendo qué comprar con las
    reglas diarias. Esto solo le da a Oscar referencias para poner órdenes límite y
    entrar en pullback en vez de pagar el open. Se calcula una vez, al cierre, desde
    las barras diarias — sin motor nocturno.

    Pullback = niveles POR DEBAJO del último cierre (no perseguir el precio). El stop
    es una ESTIMACIÓN (chandelier sobre el cierre de hoy como proxy del high de entrada).
    Devuelve None si faltan datos, o {ref, levels:[(label, price)...], stop}.
    """
    if not ind:
        return None
    ref = ind["price"]
    cands = [
        ("EMA20", ind.get("ema20")),
        ("Retest breakout", ind.get("prior_high")),
        ("Soporte 10d", ind.get("support")),
    ]
    levels = [(lbl, round(v, 2)) for lbl, v in cands if v and v < ref]
    levels.sort(key=lambda x: -x[1])                     # más cercano al precio primero
    stop = chandelier_stop(ref, ind["atr"]) if ind.get("atr") else None
    return {"ref": round(ref, 2), "levels": levels,
            "stop": round(stop, 2) if stop else None}
