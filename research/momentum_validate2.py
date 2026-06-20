#!/usr/bin/env python3
"""
MOMENTUM — validación ronda 2 (antes de construir el brazo).
Foco en los saltos GRANDES (>=4-5% anormal/30m + RVOL) donde la ronda 1 vio edge.

Responde 3 preguntas de viabilidad:
  1. SENSIBILIDAD A FEES: ¿a qué costo round-trip muere el edge? (Binance 0.05 vs eToro 0.2-1.0)
  2. VENUE: ¿qué tickers del edge están en Binance perp (fee bajo) vs solo eToro/mercado?
  3. ROBUSTEZ: ¿el edge viene de pocos tickers/días (frágil) o está repartido?

Usa barras 1m cacheadas. READ-ONLY. Uso: python research/momentum_validate2.py
"""
from __future__ import annotations
import os, sys, glob
import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
CACHE = os.path.join(_ROOT, "data", "bars_cache")

LOOKBACK = 30
RVOL_MIN = 2.0
THRS = [4.0, 5.0]
H = 120                # horizonte ganador de la ronda 1
COOLDOWN = 30
BENCH = "QQQ"

try:
    import ccxt
    _mk = ccxt.binanceusdm({"enableRateLimit": True}).load_markets()
    def has_perp(t): return f"{t}/USDT:USDT" in _mk
except Exception:
    def has_perp(t): return False


def load(sym):
    p = os.path.join(CACHE, f"{sym}_1m.parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    df = df[["c", "v"]].copy()
    df["etday"] = (df.index - pd.Timedelta(hours=4)).date
    return df


def collect():
    qqq = load(BENCH)
    qc = qqq["c"]
    files = sorted(glob.glob(os.path.join(CACHE, "*_1m.parquet")))
    tickers = [os.path.basename(f).replace("_1m.parquet", "") for f in files if BENCH not in f]
    trades = []   # (thr, ticker, day, direction, fwd_raw)
    for tk in tickers:
        df = load(tk)
        if df is None or len(df) < 100:
            continue
        df["qc"] = qc.reindex(df.index, method="ffill")
        df = df.dropna(subset=["qc"])
        for day, g in df.groupby("etday"):
            c = g["c"].values; q = g["qc"].values; v = g["v"].values
            n = len(g)
            mv = np.median(v[v > 0]) if (v > 0).any() else 0
            last = -10**9
            for t in range(LOOKBACK, n):
                if t - last < COOLDOWN or mv <= 0 or t + H >= n:
                    continue
                abn = (c[t] / c[t-LOOKBACK]-1)*100 - (q[t]/q[t-LOOKBACK]-1)*100
                rvol = v[t-LOOKBACK:t].sum() / (mv*LOOKBACK)
                if rvol < RVOL_MIN or abs(abn) < min(THRS):
                    continue
                last = t
                direction = "UP" if abn > 0 else "DOWN"
                sign = 1 if direction == "UP" else -1
                fwd_raw = sign * (c[t+H]/c[t]-1)*100
                for thr in THRS:
                    if abs(abn) >= thr:
                        trades.append((thr, tk, str(day), direction, fwd_raw))
    return trades


def main():
    tr = collect()
    print(f"Trades (>=4% , H={H}m): {len(tr)}  | perp Binance cargado: {sum(1 for t in [1] )>=0}\n")

    for thr in THRS:
        sub = [t for t in tr if t[0] == thr]
        if not sub:
            continue
        raw = np.array([t[4] for t in sub])
        print(f"=== THR {thr:.0f}% — n={len(sub)}  (UP {sum(1 for t in sub if t[3]=='UP')}/DOWN {sum(1 for t in sub if t[3]=='DOWN')}) ===")
        print("  Sensibilidad a fee round-trip:")
        for fee in (0.05, 0.10, 0.20, 0.50, 1.0):
            net = raw - fee
            print(f"    fee {fee:>4.2f}% -> fwd_net medio {net.mean():>+6.3f}%  win {100*(net>0).mean():>3.0f}%  "
                  f"mediana {np.median(net):>+6.3f}%")
        # venue
        perp = [t for t in sub if has_perp(t[1])]
        nonp = [t for t in sub if not has_perp(t[1])]
        print(f"  Venue: en Binance perp {len(perp)} trades | solo eToro/mercado {len(nonp)}")
        if perp:
            print(f"    edge perp (fee 0.05): {np.mean([t[4] for t in perp])-0.05:>+.3f}%  "
                  f"win {100*np.mean([(t[4]-0.05)>0 for t in perp]):.0f}%")
        if nonp:
            print(f"    edge no-perp (fee 0.50): {np.mean([t[4] for t in nonp])-0.50:>+.3f}%  "
                  f"win {100*np.mean([(t[4]-0.50)>0 for t in nonp]):.0f}%")
        # concentración por ticker
        from collections import defaultdict
        byt = defaultdict(list)
        for t in sub:
            byt[t[1]].append(t[4])
        print("  Concentración (top tickers por nº trades):")
        for tk, vals in sorted(byt.items(), key=lambda x: -len(x[1]))[:8]:
            print(f"    {tk:<6} n={len(vals):>3} fwd_medio {np.mean(vals):>+6.2f}% "
                  f"{'[perp]' if has_perp(tk) else ''}")
        print()


if __name__ == "__main__":
    main()
