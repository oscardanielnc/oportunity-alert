import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
BACKTEST MOMENTUM DIARIO — LONG only, con comisiones eToro.

Estrategia (trend-following clasico, defendible academicamente):
  Regimen : solo LONG si close > SMA(regime)         -> operar a favor de tendencia mayor
  Entrada : close hace nuevo maximo de cierre de N dias (breakout/continuacion)
  Salida  : trailing chandelier = (max high desde entrada) - mult*ATR
  Ejecucion: entradas/salidas al OPEN del dia siguiente (sin lookahead)
  Comision: LONG $2 fijo round-trip (= fee% segun tamano posicion). SIN shorts.

Benchmark honesto: compara contra BUY & HOLD del mismo ticker en la misma ventana.
El valor del sistema no es ganar dinero en un ganador elegido a posteriori,
sino capturar tendencias en un universo amplio y limitar drawdown.
"""
import os, sys, statistics
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests

ALPACA_BASE = "https://data.alpaca.markets"

# Universo amplio: lideres + nombres variados (evita sesgo de supervivencia)
UNIVERSE = ["NVDA","AMD","MU","PLTR","IONQ","QBTS","APP","RDDT","AVGO","MSFT",
            "DELL","SMCI","TSLA","META","GOOGL","AMZN","AAPL","NFLX","COIN","MRVL","ANET","CRWD"]

START = "2022-01-01T00:00:00Z"
POSITION_USD = 2000.0          # fee LONG = $2 round-trip = 0.10% a este tamano
LONG_FEE_USD = 2.0


def hdrs():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def fetch_daily(ticker, start=START):
    bars, params = [], {"timeframe":"1Day","start":start,"feed":"sip","limit":1000,
                        "sort":"asc","adjustment":"all"}  # ajustado por splits+dividendos
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
             "l":float(b["l"]),"c":float(b["c"]),"v":int(b["v"])} for b in bars]


def sma(vals, n, i):
    if i+1 < n: return None
    return sum(vals[i-n+1:i+1]) / n


def atr(bars, n, i):
    if i < n: return None
    trs = []
    for j in range(i-n+1, i+1):
        trs.append(max(bars[j]["h"]-bars[j]["l"],
                       abs(bars[j]["h"]-bars[j-1]["c"]),
                       abs(bars[j]["l"]-bars[j-1]["c"])))
    return sum(trs)/n


def simulate(bars, regime=200, breakout=50, atr_n=22, mult=3.0):
    """LONG-only trend-following. Returns (trades, daily_equity_strat, window_idx)."""
    closes = [b["c"] for b in bars]
    n = len(bars)
    warm = max(regime, breakout, atr_n) + 1
    if n <= warm + 5:
        return [], [], (0, 0)

    fee_frac = LONG_FEE_USD / POSITION_USD     # round-trip como fraccion
    trades = []
    equity = 1.0                                # realizado
    daily_eq = []
    pos = None                                  # {entry, hh, entry_t}

    for i in range(warm, n):
        b = bars[i]
        # --- gestion de salida (decision con datos hasta close[i], ejecuta open[i+1]) ---
        if pos:
            pos["hh"] = max(pos["hh"], b["h"])
            a = atr(bars, atr_n, i) or 0.0
            stop = pos["hh"] - mult * a
            exit_now = b["c"] < stop
            if exit_now and i+1 < n:
                xp = bars[i+1]["o"]
                gross = xp/pos["entry"] - 1.0
                net = (1+gross)*(1-fee_frac) - 1.0
                equity *= (1+net)
                trades.append({"in_t":pos["entry_t"],"out_t":bars[i+1]["t"],
                               "in":pos["entry"],"out":xp,
                               "gross_pct":gross*100,"net_pct":net*100})
                pos = None
                daily_eq.append(equity)
                continue

        # --- mark-to-market diario ---
        daily_eq.append(equity * (b["c"]/pos["entry"]) if pos else equity)

        # --- entrada (close[i] confirma, ejecuta open[i+1]) ---
        if not pos and i+1 < n:
            sma_r = sma(closes, regime, i)
            prior_high = max(closes[i-breakout:i]) if i >= breakout else None
            if sma_r and prior_high and b["c"] > sma_r and b["c"] >= prior_high:
                ep = bars[i+1]["o"]
                pos = {"entry":ep, "hh":bars[i+1]["h"], "entry_t":bars[i+1]["t"]}

    # cierre forzado al final
    if pos:
        xp = bars[-1]["c"]
        gross = xp/pos["entry"] - 1.0
        net = (1+gross)*(1-fee_frac) - 1.0
        equity *= (1+net)
        trades.append({"in_t":pos["entry_t"],"out_t":bars[-1]["t"]+"*",
                       "in":pos["entry"],"out":xp,
                       "gross_pct":gross*100,"net_pct":net*100})
        daily_eq.append(equity)

    return trades, daily_eq, (warm, n)


def max_dd(eq):
    peak, dd = -1e9, 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v/peak - 1.0)
    return dd*100


def stats_for(ticker, bars, **params):
    trades, daily_eq, (w, n) = simulate(bars, **params)
    if not trades:
        return None
    nets = [t["net_pct"] for t in trades]
    wins = [x for x in nets if x > 0]
    strat_total = (daily_eq[-1]-1)*100 if daily_eq else 0.0
    bh = (bars[-1]["c"]/bars[w]["c"]-1)*100 if n > w else 0.0
    bh_eq = [bars[i]["c"]/bars[w]["c"] for i in range(w, n)]   # equity buy&hold
    bh_dd = max_dd(bh_eq)
    # tiempo en mercado: fraccion de dias con posicion ~ trades*avg_hold/total; aprox via daily marks != equity
    return {
        "ticker": ticker, "n": len(trades),
        "wr": round(len(wins)/len(trades)*100),
        "avg_net": round(sum(nets)/len(nets), 2),
        "avg_win": round(sum(wins)/len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(x for x in nets if x<=0)/max(1,len(nets)-len(wins)), 2),
        "best": round(max(nets), 1), "worst": round(min(nets), 1),
        "strat_total": round(strat_total, 1),
        "buyhold": round(bh, 1),
        "maxdd": round(max_dd(daily_eq), 1),
        "bh_maxdd": round(bh_dd, 1),
        "years": round((n-w)/252, 1),
    }


def run():
    print(f"\n{'='*104}")
    print(f"  BACKTEST MOMENTUM DIARIO — LONG only, comisiones eToro ($2/trade @ ${POSITION_USD:.0f} pos)")
    print(f"  Estrategia: SMA200 regimen + breakout 50d + chandelier 3xATR  |  ejecucion open dia+1")
    print(f"  Benchmark: BUY & HOLD del ticker en la misma ventana")
    print(f"{'='*104}\n")

    cfg = dict(regime=200, breakout=50, atr_n=22, mult=3.0)
    bars_by = {}
    rows = []
    print(f"  {'Ticker':<6} {'Yrs':>4} {'Trades':>6} {'WR':>4} {'AvgNet':>7} {'AvgWin':>7} {'AvgLoss':>8} "
          f"{'Best':>6} {'Worst':>7} {'STRAT':>8} {'B&H':>8} {'sDD':>6} {'bhDD':>6}")
    print(f"  {'-'*104}")
    for tk in UNIVERSE:
        bars = fetch_daily(tk)
        bars_by[tk] = bars
        if len(bars) < 260:
            print(f"  {tk:<6}  historia corta ({len(bars)} dias)"); continue
        s = stats_for(tk, bars, **cfg)
        if not s:
            print(f"  {tk:<6}  sin trades"); continue
        rows.append(s)
        print(f"  {s['ticker']:<6} {s['years']:>4} {s['n']:>6} {s['wr']:>3}% {s['avg_net']:>+6.2f}% "
              f"{s['avg_win']:>+6.2f}% {s['avg_loss']:>+7.2f}% {s['best']:>+5.0f}% {s['worst']:>+6.0f}% "
              f"{s['strat_total']:>+7.0f}% {s['buyhold']:>+7.0f}% {s['maxdd']:>+5.0f}% {s['bh_maxdd']:>+5.0f}%")

    if rows:
        print(f"  {'-'*100}")
        mean_strat = statistics.mean(r["strat_total"] for r in rows)
        mean_bh    = statistics.mean(r["buyhold"] for r in rows)
        med_strat  = statistics.median(r["strat_total"] for r in rows)
        med_bh     = statistics.median(r["buyhold"] for r in rows)
        mean_dd    = statistics.mean(r["maxdd"] for r in rows)
        mean_bh_dd = statistics.mean(r["bh_maxdd"] for r in rows)
        beat = sum(1 for r in rows if r["strat_total"] > r["buyhold"])
        less_dd = sum(1 for r in rows if r["maxdd"] > r["bh_maxdd"])  # menos negativo = mejor
        tot_tr = sum(r["n"] for r in rows)
        wr_all = statistics.mean(r["wr"] for r in rows)
        print(f"  UNIVERSO ({len(rows)} tickers, {tot_tr} trades):")
        print(f"    Retorno  medio  STRAT {mean_strat:>+7.0f}%  vs  B&H {mean_bh:>+7.0f}%")
        print(f"    Retorno mediana STRAT {med_strat:>+7.0f}%  vs  B&H {med_bh:>+7.0f}%")
        print(f"    Drawdown medio  STRAT {mean_dd:>+7.0f}%  vs  B&H {mean_bh_dd:>+7.0f}%")
        print(f"    WR medio {wr_all:.0f}%  |  Strat supera retorno B&H en {beat}/{len(rows)}  |  "
              f"Strat tiene MENOR drawdown en {less_dd}/{len(rows)}")

    # ── Grid de robustez (agregado) ───────────────────────────────────────────
    print(f"\n  {'-'*100}")
    print(f"  ROBUSTEZ — retorno medio del universo por configuracion (net):")
    print(f"  {'breakout':>9} {'mult':>5} {'AvgStrat%':>10} {'MedStrat%':>10} {'WR':>5} {'AvgDD%':>7} {'>B&H':>6}")
    print(f"  {'-'*60}")
    for bo in (20, 50, 100):
        for ml in (2.5, 3.0, 4.0):
            rr = []
            for tk in UNIVERSE:
                bars = bars_by.get(tk, [])
                if len(bars) < 260: continue
                s = stats_for(tk, bars, regime=200, breakout=bo, atr_n=22, mult=ml)
                if s: rr.append(s)
            if not rr: continue
            ms = statistics.mean(r["strat_total"] for r in rr)
            md = statistics.median(r["strat_total"] for r in rr)
            wr = statistics.mean(r["wr"] for r in rr)
            dd = statistics.mean(r["maxdd"] for r in rr)
            beat = sum(1 for r in rr if r["strat_total"] > r["buyhold"])
            print(f"  {bo:>9} {ml:>5.1f} {ms:>+9.0f}% {md:>+9.0f}% {wr:>4.0f}% {dd:>+6.0f}% {beat:>3}/{len(rr)}")

    print(f"\n{'='*104}")
    print("  STRAT = retorno compuesto neto de la estrategia. BUY&HOLD = comprar y aguantar misma ventana.")
    print("  La pregunta clave: ¿la estrategia da buen retorno con MUCHO menor drawdown que aguantar?")
    print(f"{'='*104}\n")


if __name__ == "__main__":
    run()
