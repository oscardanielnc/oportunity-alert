#!/usr/bin/env python3
"""
DIGEST del scoreboard — lectura semanal para el veredicto de afinado.

Lee la tabla signal_outcomes (de una copia de metrics.db) y resume, por brazo + categoría +
fuente: nº señales, win-rate direccional, reacción anormal mediana (4h/24h), MFE — para decidir
qué SEÑALES/FUENTES funcionan y cuáles suprimir (<1% impacto). + vista por event_key (la cesta
de market movers) y candidatos a afinar.

READ-ONLY. Uso semanal:
  1) Traer la DB de la VM:  scp opc@213.35.121.9:/home/opc/oportunity-alert/data/metrics.db data/metrics_vm.db
  2) python research/scoreboard_digest.py --db data/metrics_vm.db --days 7
"""
from __future__ import annotations
import os, sys, csv, sqlite3, argparse, statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Cargar credenciales del .env del repo (TWILIO_TO/CALLMEBOT para --notify); igual que los runners.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except Exception:
    pass


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(st.median(vals), 2) if vals else None


def _send_notify(text):
    """Envía el resumen por el canal configurado (CallMeBot/WhatsApp/SMS). Para el cron semanal."""
    try:
        sys.path.insert(0, _ROOT)
        from alerts.twilio_sms import send_raw_message
        to = os.environ.get("TWILIO_TO", "")
        if not to:
            print("[notify] sin TWILIO_TO"); return False
        return send_raw_message(text, to)
    except Exception as e:
        print(f"[notify] error: {e}"); return False


def _notify_text(rows, closed, g, days, min_n):
    """Resumen compacto + recordatorio de pendientes #2/#3 (readiness por categoría)."""
    n_open = sum(1 for r in rows if r["status"] == "open")
    L = [f"📊 *Scoreboard {days}d*: {len(closed)} cerradas · {n_open} abiertas"]
    if not closed:
        L.append("Aún sin cierres — acumulando. (PED/pre-run-up tardan ~5-8d.)")
        return "\n".join(L)
    # top categorías por retorno al exit
    agg = {}
    for (arm, cat, src), rs in g.items():
        agg.setdefault((arm, cat), []).extend(rs)
    def _ex(rs): return _med([r["abn_final"] for r in rs if "abn_final" in r.keys()])
    def _win(rs):
        h = [r["dir_correct"] for r in rs if r["dir_correct"] is not None]
        return (100*sum(h)//len(h)) if h else None
    top = sorted(agg.items(), key=lambda kv: -(_ex(kv[1]) or -99))[:4]
    L.append("")
    for (arm, cat), rs in top:
        L.append(f"• {arm}/{cat}: n={len(rs)} win={_win(rs)}% exit={_ex(rs)}%")
    # readiness #2 (convicción data-driven): categorías con muestra suficiente
    ready = [f"{arm}/{cat}" for (arm, cat), rs in agg.items() if len(rs) >= min_n]
    if ready:
        L.append("")
        L.append(f"🔧 *#2 listo* (n≥{min_n}): {', '.join(ready)} → cablear convicción data-driven.")
    L.append("")
    L.append("📈 #3: corré el digest completo para la lectura.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "metrics_vm.db"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--notify", action="store_true", help="envía resumen + readiness por WhatsApp/SMS (cron)")
    ap.add_argument("--min-n", type=int, default=8, help="muestra mínima para marcar #2 listo")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"[ERROR] no existe {args.db}. Trae la DB de la VM (ver cabecera del script)."); sys.exit(1)

    con = sqlite3.connect(args.db); con.row_factory = sqlite3.Row
    try:
        con.execute("SELECT 1 FROM signal_outcomes LIMIT 1")
    except sqlite3.OperationalError:
        print("[INFO] la tabla signal_outcomes aún no existe / sin datos (el sistema apenas arrancó)."); return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    rows = [dict(r) for r in con.execute("SELECT * FROM signal_outcomes WHERE ts>=?", (cutoff,))]
    con.close()

    closed = [r for r in rows if r["status"] == "closed"]
    print(f"=== SCOREBOARD DIGEST — últimos {args.days} días ===")
    print(f"Señales: {len(rows)} | cerradas: {len(closed)} | abiertas: "
          f"{sum(1 for r in rows if r['status']=='open')} | sin_datos: {sum(1 for r in rows if r['status']=='no_data')}\n")
    if not closed:
        print("Aún sin señales cerradas (cada una tarda 48h). Vuelve a correrlo en unos días.")
        if args.notify:
            _send_notify(_notify_text(rows, closed, {}, args.days, args.min_n))
        return

    def hitrate(g):
        h = [r["dir_correct"] for r in g if r["dir_correct"] is not None]
        return f"{100*sum(h)//len(h)}%" if h else "—"

    # por brazo + categoría + fuente
    print(f"{'brazo':<14}{'categoría':<12}{'fuente':<16}{'n':>4}{'win':>6}{'abn4h':>7}{'abn24h':>8}{'exit':>7}{'MFE':>7}")
    g = defaultdict(list)
    for r in closed:
        g[(r["arm"], r["categoria"] or "?", r["source"] or "?")].append(r)
    for (arm, cat, src), rs in sorted(g.items(), key=lambda x: -len(x[1])):
        _ex = _med([r["abn_final"] for r in rs]) if "abn_final" in rs[0].keys() else None
        print(f"{arm[:14]:<14}{str(cat)[:12]:<12}{str(src)[:16]:<16}{len(rs):>4}{hitrate(rs):>6}"
              f"{str(_med([r['abn_4h'] for r in rs])):>7}{str(_med([r['abn_24h'] for r in rs])):>8}"
              f"{str(_ex):>7}{str(_med([r['mfe_abn'] for r in rs])):>7}")

    # candidatos a SUPRIMIR (impacto bajo): MFE mediana < 1% y win <= 50%
    print("\n=== CANDIDATOS A SUPRIMIR (MFE<1% y win<=50%, n>=5) ===")
    flagged = []
    for (arm, cat, src), rs in g.items():
        if len(rs) < 5:
            continue
        mfe = _med([r["mfe_abn"] for r in rs]); h = [r["dir_correct"] for r in rs if r["dir_correct"] is not None]
        wr = (100*sum(h)//len(h)) if h else None
        if mfe is not None and mfe < 1.0 and (wr is None or wr <= 50):
            flagged.append((arm, cat, src, len(rs), mfe, wr))
    for arm, cat, src, n, mfe, wr in sorted(flagged, key=lambda x: x[4]):
        print(f"  {arm}/{cat}/{src}: n={n} MFE={mfe} win={wr}% → poco impacto, evaluar suprimir")
    if not flagged:
        print("  (ninguno con suficiente muestra todavía)")

    # market movers: vista por event_key (la cesta de una declaración)
    mm = [r for r in closed if r["arm"] == "market_movers" and r.get("event_key")]
    if mm:
        print("\n=== MARKET MOVERS — cesta por declaración (event_key) ===")
        byk = defaultdict(list)
        for r in mm:
            byk[r["event_key"]].append(r)
        for k, rs in list(byk.items())[:10]:
            src = rs[0]["source"]; scope = rs[0].get("scope")
            tickers = ", ".join(f"{r['ticker']}{('+' if (r['abn_24h'] or 0)>0 else '')}{r['abn_24h']}%"
                                 for r in rs if r["abn_24h"] is not None)
            print(f"  [{src}/{scope}] {k}: {tickers}")

    if args.notify:
        ok = _send_notify(_notify_text(rows, closed, g, args.days, args.min_n))
        print(f"\n[notify] resumen {'enviado' if ok else 'NO enviado'}")


if __name__ == "__main__":
    main()
