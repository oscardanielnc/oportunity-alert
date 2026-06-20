#!/usr/bin/env python3
"""
BRAZO 3 — validación del edge de MOMENTUM (antes de construir nada).

Pregunta: tras un salto anormal intradía + RVOL alto, ¿el precio CONTINÚA (edge de momentum)
o REVIERTE? Mide el retorno forward NETO DE FEES. Si no hay edge, NO se construye el brazo
(lección Watcher/pre-market: scalping de momentum = whipsaw + fees).

Usa las barras 1m ya cacheadas en data/bars_cache/ (no descarga). Benchmark QQQ para lo anormal.
READ-ONLY.

Señal: en el minuto t, retorno anormal de los últimos L min >= THR y RVOL >= RV -> trigger.
Entrada en t (cierre), salida en t+H. Retorno forward CRUDO (lo que tradearías) menos fee
round-trip, y también ABNORMAL (vs QQQ) para ver si es edge real o solo beta.

Uso: python research/momentum_backtest.py
"""
from __future__ import annotations
import os, sys, glob
import numpy as np
import pandas as pd
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
CACHE = os.path.join(_ROOT, "data", "bars_cache")

LOOKBACK = 30          # min de la ventana de la señal
RVOL_MIN = 2.0         # volumen reciente vs típico
THRS = [2.0, 3.0, 5.0] # umbrales de salto anormal a probar (%)
HORIZONS = [30, 60, 120, 240]   # min forward
FEE_RT = 0.20          # fee round-trip % (eToro-ish; Binance perp ~0.05 -> ver sensibilidad)
COOLDOWN = 30          # min entre triggers del mismo ticker
BENCH = "QQQ"


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


def run():
    qqq = load(BENCH)
    if qqq is None:
        print("falta QQQ en cache"); return
    qc = qqq["c"]
    files = sorted(glob.glob(os.path.join(CACHE, "*_1m.parquet")))
    tickers = [os.path.basename(f).replace("_1m.parquet", "") for f in files if BENCH not in f]

    # acumular trades por (thr, dir)
    rec = {(thr, d): [] for thr in THRS for d in ("UP", "DOWN")}
    for tk in tickers:
        df = load(tk)
        if df is None or len(df) < 100:
            continue
        # alinear QQQ
        df["qc"] = qc.reindex(df.index, method="ffill")
        df = df.dropna(subset=["qc"])
        for day, g in df.groupby("etday"):
            if len(g) < LOOKBACK + max(HORIZONS) // 1 + 5:
                pass  # días cortos: igual intentamos, los forward faltantes se saltan
            c = g["c"].values; q = g["qc"].values; v = g["v"].values
            n = len(g)
            med_minvol = np.median(v[v > 0]) if (v > 0).any() else 0
            last_trig = -10**9
            for t in range(LOOKBACK, n):
                if t - last_trig < COOLDOWN:
                    continue
                # retorno anormal de los últimos LOOKBACK min
                rs = (c[t] / c[t - LOOKBACK] - 1) * 100
                rq = (q[t] / q[t - LOOKBACK] - 1) * 100
                abn = rs - rq
                rvol = (v[t - LOOKBACK:t].sum() / (med_minvol * LOOKBACK)) if med_minvol > 0 else 0
                if rvol < RVOL_MIN:
                    continue
                mag = abs(abn)
                if mag < min(THRS):
                    continue
                direction = "UP" if abn > 0 else "DOWN"
                sign = 1 if direction == "UP" else -1
                last_trig = t
                # registrar el mismo trigger en CADA bucket de umbral que califica
                for thr in THRS:
                    if mag < thr:
                        continue
                    for H in HORIZONS:
                        if t + H >= n:
                            continue
                        fwd_raw = (c[t + H] / c[t] - 1) * 100
                        fwd_q = (q[t + H] / q[t] - 1) * 100
                        pos_raw = sign * fwd_raw - FEE_RT      # posición en dir. del momentum, neto fee
                        pos_abn = sign * (fwd_raw - fwd_q)     # exceso vs QQQ, sin fee
                        rec[(thr, direction)].append((H, pos_raw, pos_abn))

    print(f"Tickers: {len(tickers)} | LOOKBACK {LOOKBACK}m RVOL>={RVOL_MIN} fee_rt {FEE_RT}% "
          f"cooldown {COOLDOWN}m\n")
    print(f"{'thr':>4} {'dir':<4} {'H':>4} {'n':>5} {'fwd_net%':>9} {'med%':>7} {'win%':>6} {'abn%':>7}")
    for thr in THRS:
        for d in ("UP", "DOWN"):
            rows = rec[(thr, d)]
            for H in HORIZONS:
                hh = [r for r in rows if r[0] == H]
                if len(hh) < 10:
                    continue
                raw = np.array([r[1] for r in hh]); abn = np.array([r[2] for r in hh])
                print(f"{thr:>4.0f} {d:<4} {H:>4} {len(hh):>5} {raw.mean():>+9.3f} "
                      f"{np.median(raw):>+7.3f} {100*(raw>0).mean():>5.0f}% {abn.mean():>+7.3f}")
        print()

    print("LECTURA: fwd_net% = retorno medio de la posición en dirección del momentum, NETO de fee.")
    print(" Si <= 0 -> NO hay edge de continuación (es reversión/ruido), NO construir el brazo.")
    print(" abn% = exceso vs QQQ (sin fee): separa edge real de beta de mercado.")


if __name__ == "__main__":
    run()
