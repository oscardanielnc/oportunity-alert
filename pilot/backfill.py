#!/usr/bin/env python3
"""
Backfill del piloto — replica el motor diario sobre historia para:
  1. validar que el motor ejecuta correctamente (compras/ventas/stops),
  2. sembrar un track record simulado del que el piloto continua EN VIVO.

Usa exactamente las mismas funciones que run_pilot (indicators, entry_candidates,
chandelier, macro_ok) y la misma PaperPortfolio. Ejecucion: pendiente del dia D
se llena al OPEN de D+1 (identico a la operativa real).

  python -m pilot.backfill            # ~14 meses, siembra data/pilot_state.json
"""
import os, sys, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pilot.universe import load_universe
from pilot.momentum_signals import (
    fetch_daily_batch, indicators, entry_candidates, chandelier_stop, macro_ok, REGIME,
)
from pilot.paper_portfolio import PaperPortfolio, K, CAP_INIT

LOOKBACK_DAYS = 640
WARMUP_BARS   = REGIME + 5


def run():
    uni = load_universe()
    print(f"[backfill] universo {len(uni)} | bajando {LOOKBACK_DAYS} dias...")
    data = fetch_daily_batch(uni + ["QQQ"], lookback_days=LOOKBACK_DAYS)
    qqq = data.pop("QQQ", [])
    qidx = {b["t"]: i for i, b in enumerate(qqq)}
    calendar = [b["t"] for b in qqq]              # QQQ cotiza todos los dias de mercado
    idxs = {tk: {b["t"]: i for i, b in enumerate(bars)} for tk, bars in data.items()}

    pp = PaperPortfolio({"capital_initial": CAP_INIT, "cash": CAP_INIT, "positions": {},
                         "pending": {"buys": [], "sells": []}, "closed_trades": [],
                         "equity_history": [], "last_run_date": None})

    # primer dia tradeable: cuando QQQ tiene >= WARMUP_BARS barras
    start_k = WARMUP_BARS
    traded_days = 0

    for k in range(start_k, len(calendar)):
        d = calendar[k]
        # indicadores y barras "al cierre de d" (slice hasta d)
        inds, opn, hi, cl = {}, {}, {}, {}
        for tk, bars in data.items():
            i = idxs[tk].get(d)
            if i is None or i < WARMUP_BARS: continue
            ind = indicators(bars[:i+1])
            if not ind: continue
            inds[tk] = ind
            opn[tk], hi[tk], cl[tk] = bars[i]["o"], bars[i]["h"], bars[i]["c"]
        qslice = qqq[:qidx[d]+1]

        # 1) ejecutar pendientes (de d-1) al OPEN de d
        for tk in pp.pending.get("sells", []):
            if tk in pp.positions and tk in opn:
                pp.close_position(tk, opn[tk], d)
        equity_now = pp.equity(cl)
        for tk in pp.pending.get("buys", []):
            if pp.free_slots <= 0: break
            if tk in opn and tk not in pp.positions:
                ind = inds.get(tk); atr_pct = (ind["atr"]/ind["price"]) if ind and ind["atr"] else None
                pp.open_position(tk, opn[tk], equity_now, atr_pct, d)

        # 2) salidas por stop chandelier
        new_sells = []
        for tk in list(pp.positions):
            if tk in hi: pp.update_high(tk, hi[tk])
            ind = inds.get(tk)
            if not ind or ind["atr"] is None or tk not in cl: continue
            if cl[tk] < chandelier_stop(pp.positions[tk]["hh"], ind["atr"]):
                new_sells.append(tk)

        # 3) entradas (macro + ranking + top-K)
        new_buys = []
        macro = macro_ok(qslice)
        held = set(pp.positions)
        slots = K - (len(held) - len(new_sells))
        if macro and slots > 0:
            cands = [tk for tk in entry_candidates(inds) if tk not in held and tk in cl]
            new_buys = cands[:slots]

        pp.s["pending"] = {"buys": new_buys, "sells": new_sells}
        pp.record_equity(d, cl)
        pp.s["last_run_date"] = d
        traded_days += 1

    pp.save()
    _report(pp, traded_days)


def _report(pp, days):
    ct = pp.s["closed_trades"]
    eh = pp.s["equity_history"]
    init = pp.s["capital_initial"]
    eq = eh[-1]["equity"] if eh else init
    print(f"\n=== BACKFILL COMPLETO — {days} dias de mercado simulados ===")
    print(f"Equity final ${eq:,.0f}  ({(eq/init-1)*100:+.1f}%)  | trades cerrados: {len(ct)} | "
          f"abiertos: {len(pp.positions)}")
    if ct:
        wins = [t for t in ct if t["pnl_pct"] > 0]
        wr = len(wins)/len(ct)*100
        avg_w = statistics.mean(t["pnl_pct"] for t in wins) if wins else 0
        losses = [t for t in ct if t["pnl_pct"] <= 0]
        avg_l = statistics.mean(t["pnl_pct"] for t in losses) if losses else 0
        print(f"WR {wr:.0f}% | ganancia media {avg_w:+.1f}% | perdida media {avg_l:+.1f}% | "
              f"mejor {max(t['pnl_pct'] for t in ct):+.0f}% | peor {min(t['pnl_pct'] for t in ct):+.0f}%")
        # mensual
        bym = {}
        for e in eh: bym[e["date"][:7]] = e["equity"]
        months = sorted(bym); prev = init; rets = []
        for m in months: rets.append((bym[m]/prev-1)*100); prev = bym[m]
        if rets:
            peak=-1e9; mdd=0
            for e in eh:
                peak=max(peak,e["equity"]); mdd=min(mdd,e["equity"]/peak-1)
            print(f"Mensual: prom {statistics.mean(rets):+.1f}% | mediana {statistics.median(rets):+.1f}% | "
                  f"mejor {max(rets):+.0f}% | peor {min(rets):+.0f}% | MaxDD {mdd*100:.0f}%")
        print("\nUltimos 8 trades cerrados:")
        for t in ct[-8:]:
            print(f"  {t['ticker']:6} {t['entry_date']}->{t['exit_date']}  "
                  f"${t['entry']:.2f}->${t['exit']:.2f}  {t['pnl_pct']:+.1f}%  (${t['pnl_usd']:+.0f})")
    print(f"\nPosiciones abiertas ahora: {', '.join(pp.positions) or '—'}")
    print(f"Pendiente manana -> comprar: {pp.pending['buys'] or '—'} | vender: {pp.pending['sells'] or '—'}")
    print("\nEstado sembrado en data/pilot_state.json — el piloto continua EN VIVO desde aqui.")


if __name__ == "__main__":
    run()
