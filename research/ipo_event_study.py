"""
IPO event study — ¿hay un patrón claro y explotable post-IPO?

Sobre el dataset de research/ipo_dataset.py (excess vs QQQ alineado por día de trading
relativo a t=0 = primer close). Responde:
  1. ¿Cuál es la trayectoria PROMEDIA/MEDIANA en exceso? ¿Hay fade pop->piso? ¿Lockup (~180d)?
  2. ¿Se segmenta por TAMAÑO (deal) y por HYPE (pop del día 1)?  (la pregunta de Oscar)
  3. Tradeabilidad: ¿el movimiento supera los costos eToro?
       - short:  0.3%/lado + carry overnight (~estimado %/día) sobre la ventana del trade
       - long:   0.3%/lado

Todo en ASCII (consola Windows). Uso: python -m research.ipo_event_study
"""
from __future__ import annotations
import json
import statistics as st
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATASET_JSON = ROOT / "data" / "ipo_dataset.json"

# Costos eToro (editar si cambian)
COMMISSION_SIDE = 0.30        # % por lado (dato de Oscar)
SHORT_CARRY_DAY = 0.027       # % diario aprox de overnight para short CFD (~10%/año / 365)
                             # NOTA: placeholder conservador; ajustar al fee real de eToro.

# Hitos de día de trading a reportar
CHECKPOINTS = [1, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 240]


def _col(dataset, key_day, field="excess_vs_qqq"):
    """Valores de todos los IPOs en el día de trading `key_day` (0-indexed)."""
    vals = []
    for r in dataset:
        ser = r.get(field) or []
        if key_day < len(ser) and ser[key_day] is not None:
            vals.append(ser[key_day])
    return vals


def _summ(vals):
    if not vals:
        return None
    vals_sorted = sorted(vals)
    return {
        "n": len(vals),
        "mean": round(st.mean(vals), 2),
        "median": round(st.median(vals), 2),
        "p25": round(vals_sorted[len(vals)//4], 2),
        "p75": round(vals_sorted[3*len(vals)//4], 2),
        "pct_neg": round(100*sum(1 for v in vals if v < 0)/len(vals), 1),
    }


def trough_stats(dataset, window=60):
    """Para cada IPO: día y profundidad del PISO de excess en los primeros `window` dias."""
    troughs = []
    for r in dataset:
        ser = [x for x in (r.get("excess_vs_qqq") or [])[:window] if x is not None]
        if len(ser) < 10:
            continue
        tmin = min(ser)
        tday = ser.index(tmin)
        troughs.append((r["symbol"], r.get("pop_pct"), r.get("deal_usd"), tday, tmin))
    return troughs


def print_path(dataset, label):
    print(f"\n=== TRAYECTORIA EXCESS vs QQQ (alineada t=0=1er close) | {label} | n={len(dataset)} ===")
    print(f"{'dia':>4} {'n':>4} {'media%':>8} {'mediana%':>9} {'p25':>7} {'p75':>7} {'%neg':>6}")
    for d in CHECKPOINTS:
        s = _summ(_col(dataset, d))
        if s:
            print(f"{d:>4} {s['n']:>4} {s['mean']:>8} {s['median']:>9} {s['p25']:>7} {s['p75']:>7} {s['pct_neg']:>6}")


def bucket_by(dataset, key, edges, labels):
    """Agrupa IPOs por un campo (deal_usd o pop_pct) en buckets [edges]."""
    buckets = {lab: [] for lab in labels}
    for r in dataset:
        v = r.get(key)
        if v is None:
            continue
        for i, e in enumerate(edges):
            if v < e:
                buckets[labels[i]].append(r)
                break
        else:
            buckets[labels[-1]].append(r)
    return buckets


def tradeability(dataset, entry_day, exit_day, side):
    """Estima el P&L NETO promedio de un trade entry_day->exit_day en la dirección `side`.
    short: gana si excess CAE (entry-exit). long: gana si SUBE. Resta costos eToro."""
    pnls = []
    hold = exit_day - entry_day
    for r in dataset:
        ser = r.get("excess_vs_qqq") or []
        if exit_day < len(ser) and ser[entry_day] is not None and ser[exit_day] is not None:
            move = ser[exit_day] - ser[entry_day]      # cambio de excess en la ventana
            gross = -move if side == "short" else move
            cost = 2 * COMMISSION_SIDE + (SHORT_CARRY_DAY * hold if side == "short" else 0)
            pnls.append(gross - cost)
    if not pnls:
        return None
    wins = sum(1 for p in pnls if p > 0)
    return {
        "side": side, "entry": entry_day, "exit": exit_day, "hold_d": hold,
        "n": len(pnls), "net_mean%": round(st.mean(pnls), 2),
        "net_median%": round(st.median(pnls), 2), "win%": round(100*wins/len(pnls), 1),
    }


def main():
    if not DATASET_JSON.exists():
        raise SystemExit("Falta data/ipo_dataset.json — corre research.ipo_dataset primero")
    dataset = json.loads(DATASET_JSON.read_text())
    dataset = [r for r in dataset if r.get("excess_vs_qqq")]
    print(f"IPOs en estudio: {len(dataset)}")

    # 1. Trayectoria global
    print_path(dataset, "TODOS")

    # 2a. Por TAMAÑO (deal_usd)
    print("\n########## SEGMENTACION POR TAMANO (deal) ##########")
    bz = bucket_by(dataset, "deal_usd",
                   [150e6, 500e6, 2e9],
                   ["micro <150M", "small 150-500M", "mid 500M-2B", "large >2B"])
    for lab, sub in bz.items():
        if len(sub) >= 5:
            print_path(sub, f"TAMANO {lab}")

    # 2b. Por HYPE (pop del dia 1)
    print("\n########## SEGMENTACION POR HYPE (pop dia 1) ##########")
    bp = bucket_by(dataset, "pop_pct",
                   [0, 20, 50],
                   ["pop<0 (frio)", "pop 0-20", "pop 20-50", "pop>50 (caliente)"])
    for lab, sub in bp.items():
        if len(sub) >= 5:
            print_path(sub, f"HYPE {lab}")

    # 3. Piso en los primeros 60 dias
    troughs = trough_stats(dataset, 60)
    if troughs:
        tdays = [t[3] for t in troughs]
        tmins = [t[4] for t in troughs]
        print(f"\n=== PISO en primeros 60 dias (n={len(troughs)}) ===")
        print(f"  dia del piso: mediana {int(st.median(tdays))}, media {st.mean(tdays):.0f}")
        print(f"  profundidad excess en el piso: mediana {st.median(tmins):.1f}%, media {st.mean(tmins):.1f}%")

    # 4. Tradeabilidad neta (varias ventanas y lados)
    print("\n########## TRADEABILIDAD NETA (costos eToro) ##########")
    print(f"  (commission {COMMISSION_SIDE}%/lado, carry short {SHORT_CARRY_DAY}%/dia)")
    trades = [
        ("short", 1, 20), ("short", 1, 30), ("short", 3, 20),
        ("short", 150, 200),   # ventana lockup (~180d) si hay datos
        ("long", 30, 120), ("long", 40, 180),
    ]
    hdr = f"{'side':>6} {'entry':>6} {'exit':>5} {'hold':>5} {'n':>4} {'net_med%':>9} {'net_avg%':>9} {'win%':>6}"
    print(hdr)
    for side, e, x in trades:
        t = tradeability(dataset, e, x, side)
        if t:
            print(f"{t['side']:>6} {t['entry']:>6} {t['exit']:>5} {t['hold_d']:>5} {t['n']:>4} "
                  f"{t['net_median%']:>9} {t['net_mean%']:>9} {t['win%']:>6}")


if __name__ == "__main__":
    main()
