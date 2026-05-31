#!/usr/bin/env python3
"""
VALIDACIÓN — PED (Post-Earnings Drift, E14 del playbook) neto de comisiones.

Tesis: tras un beat genuino de large-cap, los institucionales rebalancean en días →
drift Day+2→Day+7. Multi-día = sobrevive comisiones (a diferencia del scalp premarket).

Mecánica testeada (filtros price-based; los fundamentales F3/F4/F6 refinan después):
  F1: Day+1 cerró ≥ +3% (reacción del mercado al earnings)
  F5: sin gap-down intenso (open Day+1 no < -5% vs pre-close) — descarta Resaca
  Entrada: Day+2 OPEN. Salida: Day+5/6/7 CLOSE. LONG. eToro $2 fijo.

CONTROL: baseline = entrar Day+2 en TODOS los earnings (sin filtro F1) → ¿el filtro
Day+1≥+3% agrega edge sobre operar todos los earnings a ciegas?

Reusa super_backtest.fetch_all (Alpaca daily) + backtest_perticker_v2.etoro_fee_analysis.
Finnhub earnings calendar (date + hour amc/bmo). Sin spread/slippage.
"""
import os, sys, time, json
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_perticker_v2 import etoro_fee_analysis

WEEKS = 104            # 2 años de barras diarias + earnings (multi-régimen macro)
F1_THRESHOLD = 3.0     # Day+1 close >= +3%
F5_GAP_FLOOR = -5.0    # Day+1 open no peor que -5% (sin resaca intensa)

# Universo large-cap (≥$15B, F2 satisfecho por construcción) con earnings líquidos
UNIVERSE = [
    "NVDA", "AMD", "MU", "AVGO", "MSFT", "GOOG", "META", "AMZN", "AAPL", "TSM",
    "ORCL", "CRM", "ADBE", "NOW", "PANW", "CRWD", "SNOW", "NFLX", "QCOM", "INTC",
    "MRVL", "DELL", "ANET", "SMCI", "ARM", "UBER", "SHOP", "PLTR", "COIN", "ABNB",
    "MELI", "RDDT", "APP", "COHR", "ASML", "LRCX", "KLAC", "AMAT", "TXN", "MDB",
]


def fetch_earnings_events(ticker, sd, ed):
    """yfinance: historial real de fechas de earnings (multi-trimestre). hour->amc/bmo."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).get_earnings_dates(limit=24)
        if df is None or df.empty:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        out = []
        for idx in df.index:
            d = idx.strftime("%Y-%m-%d")
            if d < sd or d > today:      # solo earnings ya ocurridos dentro de la ventana
                continue
            hour = "bmo" if idx.hour < 12 else "amc"
            out.append({"date": d, "hour": hour})
        return sorted(out, key=lambda e: e["date"])
    except Exception:
        return []


def build_ped_trades(ticker, bars, events):
    dates = [b["t"][:10] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    trades = []
    for ev in events:
        E, h = ev["date"], ev["hour"]
        if E not in idx:
            continue
        ei = idx[E]
        r = ei if h == "bmo" else ei + 1     # reaction day (Day+1)
        if r - 1 < 0 or r + 6 >= len(bars):
            continue
        pre = bars[r - 1]["c"]
        if pre <= 0:
            continue
        day1_ret = (bars[r]["c"] - pre) / pre * 100
        gap = (bars[r]["o"] - pre) / pre * 100
        entry = bars[r + 1]["o"]             # Day+2 open
        if entry <= 0:
            continue
        trades.append({
            "ticker": ticker, "date": E, "hour": h or "?",
            "day1_ret": round(day1_ret, 2), "gap": round(gap, 2),
            "pnl_d5": (bars[r + 4]["c"] - entry) / entry * 100,   # Day+5
            "pnl_d6": (bars[r + 5]["c"] - entry) / entry * 100,   # Day+6
            "pnl_d7": (bars[r + 6]["c"] - entry) / entry * 100,   # Day+7
        })
    return trades


def _net(trades, horizon="pnl_d6"):
    if not trades:
        return None
    longs = [{"side": "LONG", "pnl": t[horizon]} for t in trades]
    fa = etoro_fee_analysis(longs)["long"]
    nbs = fa["net_by_position_size"]
    return {"n": fa["n"], "gross": fa["gross_avg_pnl"], "wr": fa["gross_wr_pct"],
            "net2k": nbs["2000"]["net_avg_pnl"], "net5k": nbs["5000"]["net_avg_pnl"]}


def _line(label, trades, horizon="pnl_d6"):
    s = _net(trades, horizon)
    if not s:
        print(f"  {label:<40} (n=0)"); return
    flag = "[+]" if s["net2k"] > 0 else "[-]"
    print(f"  {label:<40} n={s['n']:>3}  gross{s['gross']:>+7.2f}%  WR{s['wr']:>3}%  "
          f"NET2k{s['net2k']:>+7.2f}%  NET5k{s['net5k']:>+7.2f}% {flag}")


def run():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd, ed = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"\n{'#'*94}\n  VALIDACIÓN PED — {WEEKS} sem | {len(UNIVERSE)} large-caps | entrada D+2, salida D+5-7\n{'#'*94}")

    CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ped_cache.json")
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        all_tr = json.load(open(CACHE, encoding="utf-8"))
        print(f"  (cache: {len(all_tr)} eventos — usar --fresh para recomputar)")
    else:
        all_tr = []
        for tk in UNIVERSE:
            bars = fetch_all(tk, "1Day", start, limit=2000)
            if len(bars) < 30:
                print(f"  {tk:<6} sin datos diarios"); continue
            evs = fetch_earnings_events(tk, sd, ed)
            time.sleep(0.15)
            tr = build_ped_trades(tk, bars, evs)
            all_tr += tr
            print(f"  {tk:<6} {len(evs)} earnings -> {len(tr)} eventos válidos")
        json.dump(all_tr, open(CACHE, "w", encoding="utf-8"))

    # ── CONTROL vs FILTRO ──
    print(f"\n{'='*94}\n  ¿EL FILTRO PED AGREGA EDGE?  (salida Day+6, LONG, neto fees)\n{'='*94}")
    f1 = [t for t in all_tr if t["day1_ret"] >= F1_THRESHOLD]
    f1f5 = [t for t in f1 if t["gap"] >= F5_GAP_FLOOR]
    neg = [t for t in all_tr if t["day1_ret"] < 0]
    _line("BASELINE — todos los earnings", all_tr)
    _line(f"Day+1 NEGATIVO (no-PED, control)", neg)
    _line(f"F1: Day+1 >= +{F1_THRESHOLD:.0f}%", f1)
    _line("F1 + F5 (sin resaca) = PED", f1f5)

    # ── PED por horizonte de salida ──
    print(f"\n{'='*94}\n  PED (F1+F5) POR HORIZONTE DE SALIDA\n{'='*94}")
    for hz, lab in [("pnl_d5", "salida Day+5"), ("pnl_d6", "salida Day+6"), ("pnl_d7", "salida Day+7")]:
        _line(lab, f1f5, hz)

    # ── ¿concentrado en pocos tickers? ──
    print(f"\n{'='*94}\n  PED por ticker (concentración) — salida Day+6\n{'='*94}")
    from collections import defaultdict
    byt = defaultdict(list)
    for t in f1f5:
        byt[t["ticker"]].append(t["pnl_d6"])
    for tk in sorted(byt, key=lambda k: -sum(byt[k])):
        ps = byt[tk]
        print(f"  {tk:<6} n={len(ps):>2}  sum={sum(ps):>+7.2f}%  avg={sum(ps)/len(ps):>+6.2f}%")

    print(f"\n  Lectura: si F1+F5 (PED) es [+] y BATE al baseline y al Day+1-negativo, el drift es real.")
    print(f"  (Filtros price-based; faltan F3/F4/F6 fundamentales. Sin spread/slippage.)\n")


if __name__ == "__main__":
    run()
