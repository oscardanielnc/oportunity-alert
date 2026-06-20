#!/usr/bin/env python3
"""
Valida el umbral de SMS del nuevo signal_score (brazo NOTICIAS).

Para cada alerta histórica: computa el nuevo score (categoria+frescura+confirmacion) y lo cruza
con su reacción medida (MFE anormal, abn4h, dir_ok). Pregunta: ¿score>=6 separa bien lo que mueve
(>=1%) de lo que no? ¿Es 6 el corte correcto, o debería ser otro?

READ-ONLY. Cruza alerts_meta.csv + news_reactions_backfill.csv. Uso: python research/validate_sms_threshold.py
"""
from __future__ import annotations
import os, sys, csv, statistics as st
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from utils.signal_score import classify_category, CATEGORY_POINTS, freshness_points

META = os.path.join(_ROOT, "data", "alerts_meta.csv")
REAC = os.path.join(_ROOT, "data", "news_reactions_backfill.csv")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def confirm_pts_proxy(runup_abn, direction):
    """Proxy de confirmacion de precio con el run-up pre-t0 (move antes de la alerta)."""
    r = _f(runup_abn)
    if r is None:
        return 1
    d = (direction or "").upper()
    if d == "LONG":
        return 2 if r >= 0.5 else (0 if r <= -0.5 else 1)
    if d == "SHORT":
        return 2 if r <= -0.5 else (0 if r >= 0.5 else 1)
    return 1


def main():
    meta = {r["id"]: r for r in csv.DictReader(open(META, encoding="utf-8"))}
    reac = {r["alert_id"]: r for r in csv.DictReader(open(REAC, encoding="utf-8"))}

    rows = []
    for aid, m in meta.items():
        r = reac.get(aid)
        if not r or r.get("status") != "closed":
            continue
        cat = classify_category(f"{m.get('resumen','')} {m.get('titular','')} {m.get('url','')}")
        score = (CATEGORY_POINTS.get(cat, 2) + freshness_points(m.get("age_minutes"))
                 + confirm_pts_proxy(r.get("runup_pre2h_abn"), r.get("direccion")))
        rows.append({"score": score, "cat": cat,
                     "mfe": _f(r.get("mfe_abn")), "abn4h": _f(r.get("abn_240m")),
                     "dir_ok": r.get("direccion_correcta")})

    print(f"Alertas con reacción medida: {len(rows)}\n")
    print(f"{'score':>5} {'n':>4} {'MFE_med':>8} {'abn4h_med':>10} {'dir_ok':>7} {'%MFE>=1':>8} {'%MFE>=2':>8}")
    by = defaultdict(list)
    for r in rows:
        by[r["score"]].append(r)
    for s in sorted(by):
        g = by[s]
        mfes = [r["mfe"] for r in g if r["mfe"] is not None]
        hit = [1 if r["dir_ok"] == "True" else 0 for r in g if r["dir_ok"] in ("True", "False")]
        p1 = 100 * sum(1 for x in mfes if x >= 1) // len(mfes) if mfes else 0
        p2 = 100 * sum(1 for x in mfes if x >= 2) // len(mfes) if mfes else 0
        print(f"{s:>5} {len(g):>4} {st.median(mfes):>+8.2f} "
              f"{st.median([r['abn4h'] for r in g if r['abn4h'] is not None]):>+10.2f} "
              f"{(str(100*sum(hit)//len(hit))+'%') if hit else '—':>7} {str(p1)+'%':>8} {str(p2)+'%':>8}")

    print("\n=== comparación de cortes (enviar si score>=K) ===")
    print(f"{'K':>3} {'envia_n':>8} {'%total':>7} {'MFE_med_env':>12} {'dir_ok_env':>11} {'suprime_MFE>=2':>14}")
    n_tot = len(rows)
    for K in range(4, 11):
        env = [r for r in rows if r["score"] >= K]
        sup = [r for r in rows if r["score"] < K]
        mfes = [r["mfe"] for r in env if r["mfe"] is not None]
        hit = [1 if r["dir_ok"] == "True" else 0 for r in env if r["dir_ok"] in ("True", "False")]
        # cuántos buenos (MFE>=2) se SUPRIMIRÍAN con este corte (falsos negativos)
        sup_good = sum(1 for r in sup if r["mfe"] is not None and r["mfe"] >= 2)
        mm = f"{st.median(mfes):+.2f}" if mfes else "—"
        hh = f"{100*sum(hit)//len(hit)}%" if hit else "—"
        print(f"{K:>3} {len(env):>8} {100*len(env)//n_tot:>6}% {mm:>12} {hh:>11} {sup_good:>14}")


if __name__ == "__main__":
    main()
