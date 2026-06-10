"""
Dashboard de métricas — CLI.
Uso: python show_metrics.py [--days N]
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta

LIMA = timezone(timedelta(hours=-5))


def _pct_bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "#" * filled + "." * (width - filled)


def _outcome_emoji(outcome: str) -> str:
    return "WIN" if outcome == "WIN" else "LOSS"


def main():
    parser = argparse.ArgumentParser(description="OportunityAlert — Metrics Dashboard")
    parser.add_argument("--days", type=int, default=30, help="Rango de días (default: 30)")
    args = parser.parse_args()

    try:
        from utils.metrics_store import MetricsStore
        m = MetricsStore()
    except Exception as e:
        print(f"Error abriendo metrics DB: {e}")
        sys.exit(1)

    now = datetime.now(LIMA).strftime("%d/%m/%Y %H:%M Lima")
    sep = "=" * 60

    print(f"\n{sep}")
    print(f"  OPPORTUNITY ALERT — Métricas  ({now})")
    print(f"  Rango: últimos {args.days} días")
    print(sep)

    # ── Stats globales ──��─────────────────────────────────────────────────────
    stats = m.get_summary_stats()
    a = stats["alerts"]
    g = stats["gates"]
    t = stats["trades"]

    a_total = a["total"] or 0
    g_total = g["total"] or 0
    g_filt  = g["filtered"] or 0
    g_fpct  = g["filter_pct"] or 0.0
    t_total = t["total"] or 0

    print("\n[STATS] RESUMEN GLOBAL")
    print(f"  Alertas generadas : {a_total:>6}  (ALTA: {a['alta'] or 0}  MEDIA: {a['media'] or 0}  SMS: {a['sms'] or 0})")
    print(f"  Articulos a gates : {g_total:>6}")
    print(f"  Filtrados por gates: {g_filt:>5}  ({g_fpct:.1f}% ahorro de tokens IA)")
    print(f"  Trades cerrados   : {t_total:>6}")

    if t_total:
        wins = t["wins"] or 0
        win_rate = round(100 * wins / t_total, 1)
        print(f"  Win rate          : {win_rate:>5.1f}%  ({wins} wins / {t_total - wins} losses)")
        print(f"  P&L total         : ${(t['total_pnl'] or 0):>+8.2f}")
        print(f"  P&L promedio/trade: {(t['avg_pct'] or 0):>+6.1f}%")
        print(f"  Mejor trade       : {(t['best_pct'] or 0):>+6.1f}%")
        print(f"  Peor trade        : {(t['worst_pct'] or 0):>+6.1f}%")

    # ── Eficiencia de gates ───────────────────────────────────────────────────
    print(f"\n[GATES] EFICIENCIA GATES — últimos {min(args.days, 7)} días")
    gate_rows = m.get_gate_efficiency(days=min(args.days, 7))
    if gate_rows:
        print(f"  {'Fecha':<12} {'Artículos':>10} {'Filtrados':>10} {'IA calls':>9} {'% ahorro':>9}")
        print(f"  {'-'*54}")
        for r in gate_rows:
            bar = _pct_bar(r["filter_pct"] or 0, 12)
            print(
                f"  {r['date_lima']:<12} {r['total']:>10} {r['filtered']:>10} "
                f"{r['ai_called']:>9} {r['filter_pct']:>7.1f}% {bar}"
            )
    else:
        print("  Sin datos todavía")

    # ── Accuracy: alertas vs trades ───────────────────────────────────────────
    print(f"\n[ACCURACY] ACCURACY — alertas vs trades reales (últimos {args.days} días)")
    acc_rows = m.get_accuracy_summary(days=args.days)
    if acc_rows:
        # Agrupar por prioridad
        by_prio: dict = {}
        for r in acc_rows:
            p = r["prioridad"] or "?"
            if p not in by_prio:
                by_prio[p] = {"wins": 0, "losses": 0, "total_pnl": 0, "avg_pct": 0}
            if r["outcome"] == "WIN":
                by_prio[p]["wins"] += r["cnt"]
            else:
                by_prio[p]["losses"] += r["cnt"]
            by_prio[p]["total_pnl"] += r["total_pnl"] or 0

        print(f"  {'Prioridad':<10} {'Wins':>6} {'Losses':>7} {'Win%':>7} {'P&L':>10}")
        print(f"  {'-'*44}")
        for prio in ["ALTA", "MEDIA", "BAJA"]:
            if prio not in by_prio:
                continue
            d = by_prio[prio]
            total = d["wins"] + d["losses"]
            wr = round(100 * d["wins"] / total, 1) if total else 0
            print(
                f"  {prio:<10} {d['wins']:>6} {d['losses']:>7} {wr:>6.1f}% "
                f"${d['total_pnl']:>+9.2f}"
            )
    else:
        print("  Sin trades cerrados con alertas matching todavía")

    # ── P&L por estrategia (2026-06-10) ───────────────────────────────────────
    print(f"\n[ESTRATEGIA] P&L POR ESTRATEGIA — últimos {args.days} días")
    try:
        strat_rows = m.get_pnl_by_strategy(days=args.days)
    except Exception:
        strat_rows = []
    if strat_rows:
        print(f"  {'Estrategia':<12} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'P&L USD':>10} {'Avg%':>7} {'Hold(d)':>8}")
        print(f"  {'-'*62}")
        for r in strat_rows:
            total = r["trades"] or 0
            wins = r["wins"] or 0
            wr = round(100 * wins / total, 1) if total else 0
            print(
                f"  {r['strategy']:<12} {total:>7} {wins:>6} {wr:>6.1f}% "
                f"${(r['pnl_usd'] or 0):>+9.2f} {(r['avg_pct'] or 0):>+6.1f}% "
                f"{(r['avg_hold_days'] or 0):>7.1f}"
            )
        print("  (trades anteriores al 2026-06-10 aparecen como 'desconocida' — la")
        print("   columna strategy se llena desde el deploy de esa fecha)")
    else:
        print("  Sin trades con estrategia registrada todavía")

    # ── Últimos trades ────────────────────────────────────────────────────────
    print("\n[TRADES] ÚLTIMOS 10 TRADES")
    trades = m.get_recent_trades(limit=10)
    if trades:
        print(f"  {'Ticker':<6} {'Dir':<5} {'Entrada':>8} {'Salida':>8} {'P&L%':>7} {'USD':>8} {'Horas':>6} {'Alerta':<8} {'Conv':>5}")
        print(f"  {'-'*70}")
        for t in trades:
            emo = _outcome_emoji(t["outcome"] or "")
            print(
                f"  {emo}{t['ticker']:<5} {t['direction']:<5} "
                f"${t['open_rate']:>7.2f} ${t['close_rate']:>7.2f} "
                f"{t['net_profit_pct']:>+6.1f}% ${t['net_profit']:>+7.2f} "
                f"{t['hold_hours']:>5.1f}h {t['matched_prioridad'] or '—':<8} "
                f"{t['matched_conviction'] or '—':>5}"
            )
    else:
        print("  Sin trades cerrados todavía")

    print(f"\n{sep}\n")
    print("  Comandos útiles:")
    print("  python show_metrics.py --days 7    (ultima semana)")
    print("  python show_metrics.py --days 90   (ultimos 3 meses)")
    print(f"{sep}\n")


if __name__ == "__main__":
    main()
