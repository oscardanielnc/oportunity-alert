#!/usr/bin/env python3
"""Dibuja el embudo de seleccion del piloto con numeros reales del dia."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from pilot.universe import load_universe
from pilot.momentum_signals import (fetch_daily_batch, indicators, entry_candidates,
                                     macro_ok, _sma, REGIME)

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run():
    uni = load_universe()
    data = fetch_daily_batch(uni + ["QQQ"])
    qqq = data.pop("QQQ", [])
    inds = {tk: indicators(b) for tk, b in data.items()}
    inds = {k: v for k, v in inds.items() if v}
    n_uni = len(uni)
    n_above = sum(1 for v in inds.values() if v["above_sma"])
    cands = entry_candidates(inds)
    n_break = len(cands)
    top5 = cands[:5]
    macro = macro_ok(qqq)

    stages = [
        (f"1. UNIVERSO\n{n_uni} acciones mas liquidas\n(NASDAQ/NYSE, por reglas)", n_uni, "#4a4a55"),
        (f"2. FILTRO MACRO\nQQQ sobre SMA200?  {'SI' if macro else 'NO'}\n(si NO: nada se compra)", n_uni if macro else 0, "#3b6ea5"),
        (f"3. TENDENCIA\nprecio > SMA200\n{n_above} acciones", n_above, "#2e8b8b"),
        (f"4. BREAKOUT\nnuevo maximo de 50 dias\n{n_break} candidatos", n_break, "#2ca02c"),
        (f"5. RANKING + TOP 5\nlos {5} de mayor momentum\n-> {', '.join(top5)}", 5, "#1f9e1f"),
    ]

    fig, ax = plt.subplots(figsize=(11, 8))
    ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")
    maxw = 9.0
    y = 9.2
    for label, count, col in stages:
        w = max(1.6, maxw * (count / n_uni) ** 0.45) if n_uni else 1.6
        x0 = (10 - w) / 2
        ax.add_patch(FancyBboxPatch((x0, y-1.3), w, 1.25, boxstyle="round,pad=0.02,rounding_size=0.1",
                                    fc=col, ec="white", lw=1.5))
        ax.text(5, y-0.67, label, ha="center", va="center", color="white",
                fontsize=10.5, fontweight="bold")
        if y > 2:
            ax.annotate("", xy=(5, y-1.45), xytext=(5, y-1.32),
                        arrowprops=dict(arrowstyle="-|>", color="#888", lw=2))
        y -= 1.78

    ax.text(5, 9.85, "DE DONDE SALEN LAS RECOMENDACIONES (hoy)", ha="center",
            fontsize=13, fontweight="bold")
    fig.tight_layout()
    p = os.path.join(OUT, "pilot_funnel.png")
    fig.savefig(p, dpi=115); plt.close(fig)
    print("OK", p)

    # tabla de los top5
    print("\nTOP 5 DE HOY (lo que comprarias):")
    print(f"  {'Tk':<6}{'precio':>9}{'>SMA200':>9}{'breakout':>10}{'momentum':>10}")
    for tk in top5:
        i = inds[tk]
        print(f"  {tk:<6}{i['price']:>9.2f}{'SI':>9}{'SI':>10}{i['momentum']*100:>+9.0f}%")
    return p


if __name__ == "__main__":
    run()
