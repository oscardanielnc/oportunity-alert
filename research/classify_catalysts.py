#!/usr/bin/env python3
"""
FASE 0.5 — Clasifica cada noticia en su CATEGORÍA REAL de catalizador (keyword classifier sobre
resumen + titular + url) y cruza con las reacciones medidas (news_reaction_backfill) para
responder: qué catalizador mueve, cuánto y en cuánto tiempo.

READ-ONLY. Cruza por `id`/`alert_id` los dos CSV ya bajados (no re-descarga barras).

Uso: python research/classify_catalysts.py
"""
from __future__ import annotations
import os, sys, csv, re, statistics as st
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(_ROOT, "data", "alerts_meta.csv")
REAC = os.path.join(_ROOT, "data", "news_reactions_backfill.csv")
OUT = os.path.join(_ROOT, "data", "catalyst_reactions.csv")

# Categorías en orden de PRECEDENCIA (la primera que matchea gana).
# Cada una: (nombre, lista de regex sobre el texto combinado en minúsculas).
RULES = [
    ("fda_clinical",      [r"\bfda\b", r"approval", r"\bpdufa\b", r"clinical", r"phase [123]", r"ensayo", r"aprobaci[oó]n"]),
    ("contract_govt",     [r"contract", r"contrato", r"\bdod\b", r"pentagon", r"defense", r"defensa", r"\baward", r"adjudicaci", r"chips act", r"government", r"\bgobierno\b", r"\barmy\b|\bnavy\b|\bair force\b"]),
    ("offering_dilution", [r"offering", r"oferta p[uú]blica", r"diluci[oó]n", r"equity raise", r"convertible", r"registered direct", r"\bshelf\b", r"secondary offering", r"dilutiv"]),
    ("ma_rumor",          [r"\bm&a\b", r"acquisition", r"adquisici", r"\bmerger\b", r"fusi[oó]n", r"takeover", r"\bbuyout\b", r"in talks", r"en conversaciones", r"\brumor", r"\bdeal\b"]),
    ("earnings_guidance", [r"earnings", r"\beps\b", r"\brevenue\b", r"ingresos", r"\bbeat\b", r"\bmiss\b", r"gumidance|guidance", r"gu[ií]a", r"quarterly", r"trimestral", r"resultados q", r"\bq[1-4]\b"]),
    ("analyst_rating",    [r"price target", r"\bpt\b", r"precio objetivo", r"overweight", r"underweight", r"\bneutral\b", r"upgrade", r"downgrade", r"\bbuy\b", r"\bsell\b", r"outperform", r"reitera|maintains|mantiene", r"initiates|inicia cobertura", r"raises? (price )?target|sube pt|sube precio objetivo", r"analyst|analista"]),
    ("sec_vote_5_07",     [r"item 5\.07", r"votaci[oó]n", r"shareholder", r"accionistas", r"junta anual", r"annual meeting"]),
    ("sec_regfd_7_01",    [r"item 7\.01", r"regulation fd", r"reg fd"]),
    ("sec_other_8k",      [r"8-k", r"\bitem \d", r"filing", r"\bsec\b"]),
    ("spac",              [r"\bspac\b", r"acquisition corp", r"blank check"]),
    ("product_partner",   [r"partnership", r"alianza", r"colaboraci", r"collaboration", r"\blaunch", r"lanzamiento", r"\bproduct\b", r"producto", r"integraci[oó]n", r"unveils", r"presenta"]),
    ("sector_momentum",   [r"sectorial", r"\bsector\b", r"arrastra", r"momentum", r"m[aá]ximos de 52", r"52 semanas|52-week", r"optimismo"]),
]


def classify(text: str) -> str:
    t = text.lower()
    for name, pats in RULES:
        for p in pats:
            if re.search(p, t):
                return name
    return "otro"


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def main():
    if not (os.path.exists(META) and os.path.exists(REAC)):
        print(f"[ERROR] faltan {META} o {REAC}"); sys.exit(1)

    meta = {r["id"]: r for r in csv.DictReader(open(META, encoding="utf-8"))}
    reac = {r["alert_id"]: r for r in csv.DictReader(open(REAC, encoding="utf-8"))}

    rows = []
    for aid, m in meta.items():
        cat = classify(f"{m.get('resumen','')} {m.get('titular','')} {m.get('url','')}")
        r = reac.get(aid)
        rows.append({
            "id": aid, "categoria": cat,
            "priceado": m.get("priceado"), "prioridad": m.get("prioridad"),
            "tipo_alerta": m.get("tipo_alerta"), "age_min": _f(m.get("age_minutes")),
            "ticker": r.get("ticker") if r else None,
            "direccion": r.get("direccion") if r else None,
            "score_ia": r.get("score_ia") if r else None,
            "status": r.get("status") if r else "sin_reaccion",
            "abn1h": _f(r.get("abn_60m")) if r else None,
            "abn4h": _f(r.get("abn_240m")) if r else None,
            "abn24h": _f(r.get("abn_1440m")) if r else None,
            "mfe": _f(r.get("mfe_abn")) if r else None,
            "mae": _f(r.get("mae_abn")) if r else None,
            "tpeak": _f(r.get("time_to_peak_min")) if r else None,
            "dir_ok": r.get("direccion_correcta") if r else None,
            "runup2h": _f(r.get("runup_pre2h_abn")) if r else None,
            "resumen": (m.get("resumen") or "")[:90],
        })

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    ok = [r for r in rows if r["status"] == "closed" and r["abn4h"] is not None]
    print(f"Clasificadas: {len(rows)}  | con reacción medida: {len(ok)}\n")

    print("=== CATEGORÍA REAL -> reacción anormal (mediana) y velocidad ===")
    print(f"{'categoria':<18} {'n':>3} {'abn1h':>6} {'abn4h':>6} {'abn24h':>7} {'MFE':>6} {'MAE':>6} "
          f"{'dir_ok':>6} {'tpeak_h':>7} {'age_m':>6} {'%priced':>7}")
    by = defaultdict(list)
    for r in ok:
        by[r["categoria"]].append(r)
    for cat in sorted(by, key=lambda c: -len(by[c])):
        g = by[cat]
        dok = [1 if r["dir_ok"] == "True" else 0 for r in g if r["dir_ok"] in ("True", "False")]
        tp = _med([r["tpeak"] for r in g])
        print(f"{cat:<18} {len(g):>3} {_fmt(_med([r['abn1h'] for r in g])):>6} "
              f"{_fmt(_med([r['abn4h'] for r in g])):>6} {_fmt(_med([r['abn24h'] for r in g])):>7} "
              f"{_fmt(_med([r['mfe'] for r in g])):>6} {_fmt(_med([r['mae'] for r in g])):>6} "
              f"{(str(100*sum(dok)//len(dok))+'%') if dok else '—':>6} "
              f"{(f'{tp/60:.1f}' if tp is not None else '—'):>7} "
              f"{_fmt(_med([r['age_min'] for r in g]),0):>6} "
              f"{str(round(100*sum(1 for r in g if r['priceado']=='1')/len(g)))+'%':>7}")

    # cruce categoría x priceado (¿el priceado mata la reacción?)
    print("\n=== abn4h por categoría x priceado (mediana) ===")
    print(f"{'categoria':<18} {'no_priced n/abn':>18} {'priced n/abn':>16}")
    for cat in sorted(by, key=lambda c: -len(by[c])):
        g = by[cat]
        npz = [r["abn4h"] for r in g if r["priceado"] == "0"]
        pz = [r["abn4h"] for r in g if r["priceado"] == "1"]
        print(f"{cat:<18} {f'{len(npz)} / {_fmt(_med(npz))}':>18} {f'{len(pz)} / {_fmt(_med(pz))}':>16}")

    print(f"\nDetalle -> {OUT}")


def _fmt(v, dec=2):
    if v is None:
        return "—"
    return f"{v:+.{dec}f}" if dec else f"{v:.0f}"


if __name__ == "__main__":
    main()
