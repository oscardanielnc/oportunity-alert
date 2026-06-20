#!/usr/bin/env python3
"""
AUTOPSIA de los 94 saltos >=12% que el sistema NO alertó.

Cruza cada salto (misses_12.csv: ticker, onset) con article_filter_log (aflog_misses.csv:
qué artículos vimos de ese ticker y dónde murieron). Clasifica cada miss:
  - NO_VISTO        : ningún artículo en la ventana [onset-3h, onset+30m] -> cobertura/feed
  - KEYWORD_FILTER  : lo vimos pero el filtro de keywords lo mató
  - GATE1_PRICED    : conviction gate1 lo bloqueó (ya priceado)
  - AI_DISMISS      : llegó a la IA pero scoreó bajo/medio sin SMS
  - VISTO_OTRO      : visto, otro stage

Imprime el desglose + los TÍTULOS de lo que filtramos cerca de los movimientos grandes
(ahí están los catalizadores reales que matamos por error).

READ-ONLY. Uso: python research/autopsy_misses.py
"""
from __future__ import annotations
import os, sys, csv
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MISSES = os.path.join(_ROOT, "data", "misses_12.csv")
AFLOG = os.path.join(_ROOT, "data", "aflog_misses.csv")

PRE_H = 3.0      # ventana antes del onset
POST_MIN = 30    # ventana después del onset

# prioridad de "qué tan lejos llegó" en el pipeline (mayor = más lejos)
STAGE_RANK = {"keyword_filtered": 1, "gate1_blocked": 2, "ai_baja": 3, "ai_media": 4,
              "gap_sms": 5, "gap_position_sms": 5, "sent": 6}


def _utc(ts_lima: str):
    try:
        d = datetime.strptime(ts_lima[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        try:
            d = datetime.strptime(ts_lima[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return (d + timedelta(hours=5)).replace(tzinfo=timezone.utc)


def _onset(s: str):
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def classify(stage: str) -> str:
    if stage == "keyword_filtered":
        return "KEYWORD_FILTER"
    if stage == "gate1_blocked":
        return "GATE1_PRICED"
    if stage in ("ai_baja", "ai_media"):
        return "AI_DISMISS"
    if stage in ("sent", "gap_sms", "gap_position_sms"):
        return "VISTO_SENT"
    return "VISTO_OTRO"


def main():
    misses = list(csv.DictReader(open(MISSES, encoding="utf-8")))
    aflog = list(csv.DictReader(open(AFLOG, encoding="utf-8")))
    for a in aflog:
        a["_utc"] = _utc(a["ts"])
    by_ticker = defaultdict(list)
    for a in aflog:
        if a["_utc"]:
            by_ticker[a["ticker"]].append(a)

    results = []
    for m in misses:
        onset = _onset(m["onset"])
        lo, hi = onset - timedelta(hours=PRE_H), onset + timedelta(minutes=POST_MIN)
        seen = [a for a in by_ticker.get(m["ticker"], []) if lo <= a["_utc"] <= hi]
        if not seen:
            verdict = "NO_VISTO"
            best = None
        else:
            best = max(seen, key=lambda a: STAGE_RANK.get(a["stage"], 0))
            verdict = classify(best["stage"])
        results.append({**m, "verdict": verdict,
                        "n_vistos": len(seen),
                        "best_stage": best["stage"] if best else None,
                        "best_title": (best["title"] if best else "")[:80],
                        "best_source": best["source"] if best else None,
                        "best_age": best["age_minutes"] if best else None})

    print(f"=== AUTOPSIA de {len(results)} saltos >=12% perdidos ===\n")
    vc = Counter(r["verdict"] for r in results)
    for v, n in vc.most_common():
        print(f"  {v:<16} {n:>3}  ({100*n//len(results)}%)")

    print("\n=== NO_VISTO por ticker (cobertura/feed — el ticker no nos dio noticia a tiempo) ===")
    nv = [r for r in results if r["verdict"] == "NO_VISTO"]
    for t, n in Counter(r["ticker"] for r in nv).most_common():
        print(f"  {t:<6} {n}")

    print("\n=== VISTO pero MATADO — catalizadores que filtramos cerca de un +12% (muestra) ===")
    killed = [r for r in results if r["verdict"] in ("KEYWORD_FILTER", "GATE1_PRICED", "AI_DISMISS")]
    killed.sort(key=lambda r: -float(r["magnitude"]))
    for r in killed[:25]:
        print(f"  {r['ticker']:<5} {r['date']} {r['direccion']:<4} {r['magnitude']:>5}% "
              f"[{r['verdict']:<14}] {r['best_source']:<18} age={r['best_age']}\n"
              f"        \"{r['best_title']}\"")

    # guardar
    out = os.path.join(_ROOT, "data", "autopsy_misses.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys())); w.writeheader(); w.writerows(results)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
