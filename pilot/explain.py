#!/usr/bin/env python3
"""
Explica QUE evalua el piloto, con numeros reales del dia.
  python -m pilot.explain
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pilot.universe import load_universe
from pilot.momentum_signals import (fetch_daily_batch, indicators, entry_candidates,
                                     chandelier_stop, macro_ok, _sma, REGIME, BREAKOUT, MOM_LOOKBACK, MULT)
from pilot.paper_portfolio import PaperPortfolio


def run():
    uni = load_universe()
    data = fetch_daily_batch(uni + ["QQQ"])
    qqq = data.pop("QQQ", [])
    ref = qqq[-1]["t"]
    inds = {tk: indicators(b) for tk, b in data.items()}
    inds = {k: v for k, v in inds.items() if v}

    qpx = qqq[-1]["c"]; qsma = _sma([b["c"] for b in qqq], REGIME)
    print(f"\n=== QUE EVALUA EL PILOTO — cierre {ref} ===\n")
    print(f"[FILTRO MACRO]  QQQ ${qpx:.2f} vs SMA200 ${qsma:.2f}  ->  "
          f"{'ALCISTA: se permiten entradas' if qpx>qsma else 'BAJISTA: NO se abren nuevas'}\n")

    print("Para CADA ticker mira 4 cosas (todas con precios de CIERRE, ya fijos):")
    print("  1) precio > SMA200?      (tendencia mayor alcista)")
    print("  2) cierre >= max 50d?    (breakout = nuevo maximo de 50 dias)")
    print("  3) momentum 126d         (fuerza relativa, para rankear)")
    print("  4) ATR                   (para calcular el stop)\n")

    cands = entry_candidates(inds)
    print(f"--- TOP CANDIDATOS A COMPRAR (pasan filtro 1+2, ordenados por momentum) ---")
    print(f"  {'Tk':<6}{'precio':>9}{'SMA200':>9}{'>SMA?':>7}{'max50d':>9}{'breakout?':>10}{'mom6m':>8}{'ATR':>8}")
    for tk in cands[:8]:
        i = inds[tk]
        print(f"  {tk:<6}{i['price']:>9.2f}{i['sma200']:>9.2f}{'SI' if i['above_sma'] else 'no':>7}"
              f"{i['prior_high']:>9.2f}{'SI' if i['is_breakout'] else 'no':>10}"
              f"{i['momentum']*100:>+7.0f}%{i['atr']:>8.2f}")

    pp = PaperPortfolio.load()
    if pp.positions:
        print(f"\n--- POSICIONES ABIERTAS: que vigila para VENDER ---")
        print(f"  {'Tk':<6}{'entrada':>9}{'precio':>9}{'maxAlcz':>9}{'stop(4xATR)':>12}{'aStop%':>8}{'pnl%':>8}")
        for tk, p in pp.positions.items():
            i = inds.get(tk)
            if not i: continue
            stop = chandelier_stop(p["hh"], i["atr"])
            dist = (i["price"]/stop - 1)*100
            pnl = (i["price"]/p["entry_price"] - 1)*100
            print(f"  {tk:<6}{p['entry_price']:>9.2f}{i['price']:>9.2f}{p['hh']:>9.2f}"
                  f"{stop:>12.2f}{dist:>+7.1f}%{pnl:>+7.1f}%")
        print("\n  Regla: si el CIERRE cae por debajo del stop -> vender al open siguiente.")
        # 2026-06-10: corregido — el stop sigue al maxAlcanzado pero se RECALCULA con el
        # ATR de cada dia: si la volatilidad se expande (panico), el stop puede BAJAR.
        # Es la version validada por backtest (el ratchet "nunca baja" se midio y PIERDE:
        # Sharpe 1.54->1.18, MaxDD -20.7->-27.2 — vende el shakeout y recompra arriba).
        print("  El stop sigue al maxAlcanzado, pero respira con el ATR del dia:")
        print("  puede bajar si la volatilidad se expande (validado: NO es un trailing fijo).")
    print()


if __name__ == "__main__":
    run()
