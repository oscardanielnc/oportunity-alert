import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
BACKTEST DE ROTACIÓN — ¿conviene rotar a los líderes más fuertes en vez de solo
dejar correr con chandelier? (idea de Oscar 2026-06-01)

Compara, sobre EL MISMO universo, datos y comisiones que el backtest validado:

  BASE (lo que corre hoy):  chandelier 4×ATR para salir + rellenar SOLO slots libres
                            con los breakouts de mayor momentum 126d. NO rota.

  ROTACIÓN (idea Oscar):    además del chandelier, si aparece un breakout candidato con
                            momentum MAYOR que el del peor holder (× (1+histéresis)),
                            vende el peor y compra el más fuerte. "No conservar posiciones
                            que crezcan menos que los nuevos líderes."

Histéresis = umbral para evitar churn: 0% = rotación pura (agresiva), 10%/25% = más pegajosa.
Todo neto de comisiones ($1/lado = $2 round-trip), ejecución al OPEN del día siguiente.

Config = la viva: K=5, breakout=50, mult=4.0, filtro macro QQQ>SMA200.
Reporta total/CAGR/MaxDD/Sharpe/Trades/Fees + DESGLOSE POR AÑO (robustez, no un solo año).
"""
import os, sys, statistics
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

from backtest_portfolio_momentum import (
    fetch_daily, precompute, build_calendar, monthly_stats, _close_on,
    UNIVERSE, START_CAPITAL, FEE_USD,
)

K, BREAKOUT, MULT, REGIME = 5, 50, 4.0, 200


def run(data, calendar, rotation=False, hyst=0.0, index_data=None):
    """Motor único. rotation=False reproduce el BASE; True añade el swap por momentum."""
    cash = START_CAPITAL
    positions = {}          # tk -> {shares, entry, hh}
    daily_eq, trades = [], []
    fees_paid = 0.0
    warm = max(REGIME, 126, BREAKOUT) + 1

    def mom_at(tk, d):
        dd = data[tk]; i = dd["idx"].get(d)
        return dd["pre"]["mom"][i] if i is not None else None

    for d in calendar:
        # ---------- 1) salidas por chandelier (idéntico al base) ----------
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None: continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"])
            a = dd["pre"]["atr"][i]
            if a is None: continue
            if b["c"] < p["hh"] - MULT * a and i + 1 < len(dd["bars"]):
                to_exit.append(tk)
        for tk in to_exit:
            dd = data[tk]; i = dd["idx"][d]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(tk)
            cash += p["shares"] * xo - FEE_USD; fees_paid += FEE_USD
            trades.append({"tk": tk, "gross": (xo / p["entry"] - 1) * 100, "out": dd["bars"][i + 1]["t"], "kind": "chandelier"})

        # régimen macro
        regime_ok = True
        if index_data is not None:
            ii = index_data["idx"].get(d)
            if ii is not None and index_data["sma"][ii] is not None:
                regime_ok = index_data["bars"][ii]["c"] > index_data["sma"][ii]

        # candidatos breakout (no held), ordenados por momentum desc
        cands = []
        for tk in data:
            if tk in positions: continue
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i < warm or i + 1 >= len(dd["bars"]): continue
            pre = dd["pre"]; c = dd["bars"][i]["c"]
            sma, bkh, m = pre["sma"][i], pre["bkh"][i], pre["mom"][i]
            if sma and bkh and m is not None and c > sma and c >= bkh:
                cands.append((m, tk, i))
        cands.sort(reverse=True)

        # ---------- 2) ROTACIÓN: vender el peor holder si un candidato es más fuerte ----------
        if rotation and regime_ok and cands and positions:
            changed = True
            while changed and cands and positions:
                changed = False
                # peor holder por momentum (que tenga barra hoy y open mañana disponible)
                held_moms = [(mom_at(tk, d), tk) for tk in positions]
                held_moms = [(m, tk) for m, tk in held_moms if m is not None
                             and data[tk]["idx"].get(d) is not None
                             and data[tk]["idx"][d] + 1 < len(data[tk]["bars"])]
                if not held_moms: break
                wm, wtk = min(held_moms)
                bm, btk, bi = cands[0]
                if bm > wm * (1 + hyst) and bm > 0:
                    # vender el peor al open de mañana
                    dd = data[wtk]; i = dd["idx"][d]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(wtk)
                    cash += p["shares"] * xo - FEE_USD; fees_paid += FEE_USD
                    trades.append({"tk": wtk, "gross": (xo / p["entry"] - 1) * 100, "out": dd["bars"][i + 1]["t"], "kind": "rotation_sell"})
                    cands.pop(0)  # btk pasará a comprarse en el fill de abajo
                    # comprar el líder al open de mañana
                    eo = data[btk]["bars"][bi + 1]["o"]
                    equity_now = cash + sum(positions[t]["shares"] * _close_on(data[t], d) for t in positions)
                    target = min(cash, equity_now / K)
                    if target > FEE_USD + 1:
                        shares = (target - FEE_USD) / eo
                        cash -= shares * eo + FEE_USD; fees_paid += FEE_USD
                        positions[btk] = {"shares": shares, "entry": eo, "hh": data[btk]["bars"][bi + 1]["h"]}
                        trades.append({"tk": btk, "gross": 0, "out": data[btk]["bars"][bi + 1]["t"], "kind": "rotation_buy"})
                    changed = True

        # ---------- 3) rellenar slots libres (idéntico al base) ----------
        free = K - len(positions)
        if free > 0 and regime_ok:
            equity_now = cash + sum(positions[t]["shares"] * _close_on(data[t], d) for t in positions)
            for m, tk, i in [c for c in cands if c[1] not in positions][:free]:
                eo = data[tk]["bars"][i + 1]["o"]; target = min(cash, equity_now / K)
                if target <= FEE_USD + 1: continue
                shares = (target - FEE_USD) / eo
                cash -= shares * eo + FEE_USD; fees_paid += FEE_USD
                positions[tk] = {"shares": shares, "entry": eo, "hh": data[tk]["bars"][i + 1]["h"]}

        eq = cash + sum(positions[tk]["shares"] * _close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))

    return daily_eq, trades, fees_paid


def yearly(daily_eq):
    """CAGR-equiv por año (retorno del año) sobre equity diaria."""
    by_year_last, by_year_first = {}, {}
    for d, eq in daily_eq:
        y = d[:4]
        if y not in by_year_first: by_year_first[y] = eq
        by_year_last[y] = eq
    out = {}
    for y in sorted(by_year_last):
        out[y] = (by_year_last[y] / by_year_first[y] - 1) * 100
    return out


def main():
    print(f"\n{'='*100}")
    print(f"  ROTACIÓN vs BASE — K={K} brk={BREAKOUT} mult={MULT} +macro QQQ | universo {len(UNIVERSE)} | fee ${FEE_USD}/lado")
    print(f"{'='*100}")

    raw = {}
    for tk in UNIVERSE + ["QQQ"]:
        b = fetch_daily(tk)
        if len(b) > 260: raw[tk] = b
    qqq = raw.pop("QQQ", None)
    data = {tk: {"bars": bars, "idx": {b["t"]: i for i, b in enumerate(bars)},
                 "pre": precompute(bars, BREAKOUT)} for tk, bars in raw.items()}
    cal = build_calendar(data)
    idx_obj = None
    if qqq:
        idx_obj = {"bars": qqq, "idx": {b["t"]: i for i, b in enumerate(qqq)}, "sma": precompute(qqq, 50)["sma"]}

    variants = [
        ("BASE (chandelier+fill)", dict(rotation=False)),
        ("Rotación hyst 0%",       dict(rotation=True, hyst=0.00)),
        ("Rotación hyst 10%",      dict(rotation=True, hyst=0.10)),
        ("Rotación hyst 25%",      dict(rotation=True, hyst=0.25)),
    ]

    print(f"\n  {'Variante':<24} {'Total%':>8} {'CAGR%':>6} {'PeorMes':>8} {'MaxDD%':>7} {'Sharpe':>6} {'Trades':>6} {'Fees$':>7}")
    print(f"  {'-'*92}")
    rows = {}
    for label, kw in variants:
        deq, trades, fees = run(data, cal, index_data=idx_obj, **kw)
        s = monthly_stats(deq); rows[label] = (s, deq, trades, fees)
        print(f"  {label:<24} {s['total']:>+7.0f}% {s['cagr']:>+5.1f}% {s['worst_m']:>+7.1f}% "
              f"{s['mdd']:>+6.1f}% {s['sharpe']:>6.2f} {len(trades):>6} {fees:>7.0f}")

    if qqq:
        sb = monthly_stats([(b["t"], START_CAPITAL * b["c"] / qqq[0]["c"]) for b in qqq])
        print(f"  {'-'*92}")
        print(f"  {'QQQ buy&hold':<24} {sb['total']:>+7.0f}% {sb['cagr']:>+5.1f}% {sb['worst_m']:>+7.1f}% "
              f"{sb['mdd']:>+6.1f}% {sb['sharpe']:>6.2f}")

    # DESGLOSE POR AÑO (robustez: ¿gana siempre o por un solo año?)
    print(f"\n  DESGLOSE POR AÑO (retorno %):")
    years = sorted(yearly(rows['BASE (chandelier+fill)'][1]).keys())
    print(f"  {'Variante':<24} " + " ".join(f"{y:>7}" for y in years))
    print(f"  {'-'*(24+8*len(years))}")
    for label, _ in variants:
        yr = yearly(rows[label][1])
        print(f"  {label:<24} " + " ".join(f"{yr.get(y,0):>+6.0f}%" for y in years))

    print(f"\n{'='*100}")
    print("  Lectura: la rotación solo vale si MEJORA Sharpe/MaxDD NETO de fees y es ROBUSTA año a año.")
    print("  Más trades + más fees con peor o igual Sharpe = churn que destruye el edge (como el Watcher).")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    main()
