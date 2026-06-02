"""
Gap continuation study — ¿un gap fuerte de pre-market predice que SIGA subiendo en
la sesión regular? (Lección MRVL/QBTS: gap explosivo por catalizador, luego sigue.)

Insight clave: el OPEN de la sesión regular YA incorpora todo el movimiento de
pre-market/overnight. Entonces con barras DIARIAS:
  gap%        = open / prev_close - 1          (cuánto se movió pre-market)
  open->close = close / open - 1               (¿continúa en la sesión regular?)  <- la pregunta
  cont D+1/D+3= close[t+k] / close[t] - 1       (¿sigue los días siguientes?)

Mide la distribución de continuación CONDICIONADA al tamaño del gap y a si el nombre
está en tendencia (open > SMA50). Neto de fees eToro (0.3%/lado, long intradía sin carry).

Universo = top-80 líquido (pilot) UNION sectores especulativos completos (quantum,
small-AI, space, high-vol) — categorías mecánicas, NO nombres elegidos por resultado.
Bear-inclusive (2021-2025). ASCII only. Uso: python -m research.gap_continuation
"""
from __future__ import annotations
import json
import statistics as st
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
CACHE = ROOT / "data" / "gap_cache"
START = "2021-01-01"
FEE_RT = 0.6   # 0.3%/lado ida y vuelta

GAP_BUCKETS = [(5, 10), (10, 20), (20, 40), (40, 999)]   # % de gap al alza


def build_universe() -> list[str]:
    from pilot.universe import load_universe
    from utils.conviction_gates import SECTOR_FOR_ATR
    uni = set(load_universe() or [])
    uni |= set(SECTOR_FOR_ATR.keys())   # quantum/small-AI/space/high-vol completos
    # añadir el resto de cada sector que conviction_gates conoce por umbral de priceado
    from utils.conviction_gates import PRICED_THRESHOLDS
    uni |= set(PRICED_THRESHOLDS.keys())
    return sorted(t for t in uni if t.isalpha())


def fetch_bars(sym: str) -> list | None:
    cache = CACHE / f"{sym}.json"
    if cache.exists():
        d = json.loads(cache.read_text())
        return None if d.get("empty") else d["bars"]
    import yfinance as yf
    try:
        h = yf.Ticker(sym).history(start=START, auto_adjust=False)
        if h.empty or len(h) < 60:
            cache.write_text(json.dumps({"empty": True}))
            return None
        bars = [{"d": d.strftime("%Y-%m-%d"), "o": float(r.Open), "h": float(r.High),
                 "l": float(r.Low), "c": float(r.Close), "v": float(r.Volume)}
                for d, r in zip(h.index, h.itertuples())]
        cache.write_text(json.dumps({"bars": bars}))
        return bars
    except Exception as e:
        print(f"[bars] ERR {sym}: {e}")
        return None


def _sma(closes, n, i):
    if i < n:
        return None
    return sum(closes[i-n:i]) / n


def collect_gaps(universe):
    """Junta todos los eventos de gap al alza >5% con sus retornos forward."""
    events = []
    for k, sym in enumerate(universe):
        bars = fetch_bars(sym)
        if not bars:
            continue
        closes = [b["c"] for b in bars]
        for i in range(1, len(bars) - 1):
            prev_c = bars[i-1]["c"]
            o, c = bars[i]["o"], bars[i]["c"]
            if prev_c <= 0 or o <= 0:
                continue
            gap = (o - prev_c) / prev_c * 100
            if gap < 5:
                continue
            sma50 = _sma(closes, 50, i)
            ev = {
                "sym": sym, "date": bars[i]["d"], "gap": gap,
                "open_to_close": (c - o) / o * 100,            # continuación intradía
                "gap_to_close": (c - prev_c) / prev_c * 100,    # movimiento total del día
                "uptrend": (sma50 is not None and o > sma50),
            }
            # continuación multi-día (cierre del día del gap -> cierres siguientes)
            for kd in (1, 3, 5):
                if i + kd < len(bars):
                    ev[f"cont_d{kd}"] = (closes[i+kd] - c) / c * 100
            events.append(ev)
        if (k+1) % 20 == 0:
            print(f"[collect] {k+1}/{len(universe)} tickers...")
    return events


def _summ(vals):
    if not vals:
        return None
    return {"n": len(vals), "mean": round(st.mean(vals), 2),
            "median": round(st.median(vals), 2),
            "win": round(100*sum(1 for v in vals if v > 0)/len(vals), 1)}


def report(events):
    print(f"\nEventos de gap >5% al alza: {len(events)}\n")
    print("########## CONTINUACION open->close POR TAMANO DE GAP ##########")
    print("(open->close NETO de fees 0.6% ida/vuelta = comprar al open, vender al close)")
    print(f"{'gap bucket':>14} {'n':>5} {'o->c med%':>10} {'o->c avg%':>10} {'win%':>6} {'neto med%':>10}")
    for lo, hi in GAP_BUCKETS:
        sub = [e for e in events if lo <= e["gap"] < hi]
        s = _summ([e["open_to_close"] for e in sub])
        if s:
            print(f"{str(lo)+'-'+str(hi)+'%':>14} {s['n']:>5} {s['median']:>10} {s['mean']:>10} {s['win']:>6} {round(s['median']-FEE_RT,2):>10}")

    print("\n########## CONTINUACION open->close SEGUN TENDENCIA (open vs SMA50) ##########")
    for trend, lab in [(True, "EN TENDENCIA (open>SMA50)"), (False, "CONTRA-TENDENCIA")]:
        print(f"\n-- {lab} --")
        print(f"{'gap bucket':>14} {'n':>5} {'o->c med%':>10} {'o->c avg%':>10} {'win%':>6}")
        for lo, hi in GAP_BUCKETS:
            sub = [e for e in events if lo <= e["gap"] < hi and e["uptrend"] == trend]
            s = _summ([e["open_to_close"] for e in sub])
            if s and s["n"] >= 10:
                print(f"{str(lo)+'-'+str(hi)+'%':>14} {s['n']:>5} {s['median']:>10} {s['mean']:>10} {s['win']:>6}")

    print("\n########## CONTINUACION MULTI-DIA (cierre del dia del gap -> D+k) ##########")
    print("(para los gaps GRANDES >=20%: el swing de varios dias tipo MRVL/QBTS)")
    big = [e for e in events if e["gap"] >= 20]
    print(f"  gaps >=20%: n={len(big)}")
    for kd in (1, 3, 5):
        s = _summ([e[f"cont_d{kd}"] for e in big if f"cont_d{kd}" in e])
        if s:
            print(f"  D+{kd}: media {s['mean']:+.2f}% mediana {s['median']:+.2f}% win {s['win']}% (n={s['n']})")


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    uni = build_universe()
    print(f"Universo: {len(uni)} tickers")
    events = collect_gaps(uni)
    (ROOT / "data" / "gap_events.json").write_text(json.dumps(events))
    report(events)


if __name__ == "__main__":
    main()
