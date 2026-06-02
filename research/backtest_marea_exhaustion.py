import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO G — ¿COMPRAR LA CIMA? extensión + desaceleración de líderes de Marea (Tests A+B de Oscar)
================================================================================================
Preocupación de Oscar (2026-06-02): un líder #1 del top de Marea llega ahí por una racha fuerte;
¿y si compro justo en la cima de su vida y de ahí cae? Tiene el chandelier −4×ATR, pero quiere saber
si puede detectar "ya no crece fuerte como antes / está por techar" ANTES de entrar.

CONTEXTO YA VALIDADO: los líderes fuertes PERSISTEN (Estudio B fresh-breakouts: líderes >> breakouts
nuevos). Y hay 2 protecciones: chandelier −4×ATR (pérdida acotada si techa) + vol-sizing (los volátiles
entran chicos). La pregunta abierta: ¿hay una señal medible de agotamiento que prediga peor forward, y
filtrarla mejora el riesgo-ajustado? Prior: filtrar extendidos suele RESTAR (cortar líderes = quitar
ganadores, como fresh-breakouts/invalidación), pero se mide, no se asume.

MÉTRICAS EN LA ENTRADA:
  Extensión (A):  ext_atr = (precio − máx50d)/ATR (ATRs sobre el breakout) | pct_ema20 (% sobre EMA20) | RSI14
  Desaceleración (B): accel21 = mom21 − mom21(hace 21d)  (<0 = el momentum 21d se está aplanando)
                      m21_vs_pace = mom21 − mom63·(21/63)  (<0 = ritmo reciente < ritmo medio)
RESULTADO FORWARD por entrada: fwd20 (retorno a 20 días hábiles), realized (P&L del trade hasta el
chandelier), MAE (peor caída desde la entrada = cuánto dolió). Buckets por tercil + correlación.
Luego: variantes del backtest que SALTAN entradas según cada señal → ¿mejora Sharpe/MaxDD/WR?

REPRODUCIR: python backtest_marea_exhaustion.py

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02, 99 entradas / 94 cerradas — muestra chica, sugestivo) — VEREDICTO: NO existe
señal de agotamiento que prediga el techo; comprar "estirado/sobrecomprado" rindió IGUAL o MEJOR. La
preocupación es intuitiva pero la data la invierte: el momentum PERSISTE. NO filtrar.

1. CORRELACIÓN señal-entrada → resultado forward: las de EXTENSIÓN son POSITIVAS (más estirado = mejor):
   pct_ema20→fwd20 +0.42, ext_atr→fwd20 +0.31, RSI14→realized +0.20. Las de desaceleración ~0
   (accel21→realized +0.01). NINGUNA dice "esto va a caer".
2. TERCILES: el tercil MÁS extendido rinde MEJOR — pct_ema20 ALTO (+19% sobre EMA20) realized +28.9%
   vs bajo +4.9%; RSI14 ALTO (~83, sobrecompra) realized +28.3% (el mejor). "Comprar la cima" = comprar
   al líder más fuerte = lo que más rinde. (WR baja algo en el momentum más caliente pero el realized NO:
   la cola derecha manda → trend-following.)
3. FILTRAR HACE DAÑO: skip RSI>80 → Sharpe 1.58→1.29 (CAGR 40→26); skip pct_ema20>15% → 1.44; skip
   accel21<0 → 1.35; skip m21_vs_pace<0 → 1.29 y MaxDD peor. NINGÚN filtro mejora el riesgo-ajustado;
   todos cortan ganadores. (ext_atr>4/>6 ≈ no-op.)

POR QUÉ: el edge de Marea ES comprar fuerza y montarla; no se puede ni se necesita "timear el techo".
Lo que protege de comprar un pico NO es un filtro predictivo (no existe fiable) sino el CHANDELIER −4×ATR
(pérdida acotada si techa) + el VOL-SIZING (los volátiles entran chicos). Mostrar una alerta de
"sobre-extendido" sería CONTRAPRODUCENTE (empujaría a saltar ganadores). Respuesta a Oscar: no podés
saber que es la cima, y no hace falta — el chandelier lo gestiona. Consistente con fresh-breakouts e
invalidación (cortar fuerza resta). CAVEAT: n=99 (Marea hace ~15-20 entradas/año), sugestivo no airtight.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics, math
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_regime import regime_features, expo_series, START_S, CACHE_S
from backtest_marea_selection import (
    load_data, signal_val, perf, summarize, K, WARM, VOL_TARGET, FEE, CAP, MULT, DATA_DIR,
)
from backtest_portfolio_momentum import build_calendar


def ema(closes, n):
    if len(closes) < n: return None
    k = 2 / (n + 1); e = sum(closes[:n]) / n
    for c in closes[n:]: e = c * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0); losses += max(-ch, 0)
    if losses == 0: return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def emetrics(dd, i):
    closes = [b["c"] for b in dd["bars"][:i + 1]]
    price = closes[-1]; pre = dd["pre"]
    atr = pre["atr"][i]; bkh = pre["bkh"][i]
    m21 = pre["mom"][21][i]; m63 = pre["mom"][63][i]
    m21p = pre["mom"][21][i - 21] if i >= 21 else None
    e20 = ema(closes, 20)
    return {
        "ext_atr": (price - bkh) / atr if (atr and bkh) else None,
        "pct_ema20": (price / e20 - 1) * 100 if e20 else None,
        "rsi14": rsi(closes, 14),
        "accel21": (m21 - m21p) * 100 if (m21 is not None and m21p is not None) else None,
        "m21_vs_pace": (m21 - m63 * (21 / 63)) * 100 if (m21 is not None and m63 is not None) else None,
    }


def run_marea(data, cal, expo, skip=None, capture=False):
    """BASE Marea (histéresis gate + breakout + chandelier 4×ATR + vol-sizing + top-K mom126).
    skip(metrics)->bool veta una entrada (rellena con el siguiente candidato). capture: guarda entradas."""
    cash = CAP; positions = {}; daily_eq = []; trades = []; entries = []; fees = 0.0; inv = []
    for d in cal:
        e = expo.get(d, 1.0); cap_k = max(0, round(e * K))
        # salidas chandelier
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i + 1 >= len(dd["bars"]): continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"]); a = dd["pre"]["atr"][i]
            if a is not None and b["c"] < p["hh"] - MULT * a:
                to_exit.append((tk, i))
        for tk, i in to_exit:
            dd = data[tk]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(tk)
            cash += p["shares"] * xo - FEE; fees += FEE
            pnl = (xo / p["entry"] - 1) * 100
            trades.append({"pnl_pct": pnl, "held": i - p["eidx"]})
            if capture and p.get("erec") is not None:
                seg = [bb["c"] for bb in dd["bars"][p["eidx"]:i + 2]]
                mae = (min(seg) / p["entry"] - 1) * 100 if seg else 0
                p["erec"].update(realized=pnl, mae=mae)
        equity_now = cash + sum(positions[t]["shares"] * data[t]["bars"][data[t]["idx"][d]]["c"]
                                for t in positions if d in data[t]["idx"])
        # relleno
        free = cap_k - len(positions)
        if free > 0:
            cands = []
            for tk in data:
                if tk in positions: continue
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i < WARM or i + 1 >= len(dd["bars"]): continue
                c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]; bkh = dd["pre"]["bkh"][i]
                if not (sma and c > sma) or not (bkh and c >= bkh): continue
                s = signal_val(dd["pre"], i, "mom126")
                if s is not None: cands.append((s, tk, i))
            cands.sort(reverse=True)
            filled = 0
            for s, tk, i in cands:
                if filled >= free: break
                dd = data[tk]; m = emetrics(dd, i)
                if skip and skip(m): continue
                eo = dd["bars"][i + 1]["o"]
                base = equity_now / K
                a = dd["pre"]["atr"][i]; atr_pct = (a / dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET / atr_pct) if atr_pct > 0 else 1.0
                target = min(cash, base)
                if target <= FEE + 1: continue
                sh = (target - FEE) / eo; cash -= sh * eo + FEE; fees += FEE
                erec = None
                if capture:
                    fwd20 = None
                    if i + 1 + 20 < len(dd["bars"]):
                        fwd20 = (dd["bars"][i + 1 + 20]["c"] / eo - 1) * 100
                    erec = dict(m); erec.update(tk=tk, fwd20=fwd20, realized=None, mae=None)
                    entries.append(erec)
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i + 1]["h"],
                                 "eidx": i + 1, "erec": erec}
                filled += 1
        eq = cash + sum(positions[tk]["shares"] * data[tk]["bars"][data[tk]["idx"][d]]["c"]
                        for tk in positions if d in data[tk]["idx"])
        daily_eq.append((d, eq)); inv.append(len(positions) / K)
    return {"deq": daily_eq, "trades": trades, "entries": entries, "fees": fees,
            "invested": statistics.mean(inv) if inv else 0}


def corr(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 5: return 0.0
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def terciles(entries, metric, outcome):
    vals = [(e[metric], e.get(outcome)) for e in entries if e.get(metric) is not None and e.get(outcome) is not None]
    vals.sort()
    n = len(vals)
    if n < 9: return None
    third = n // 3
    groups = [vals[:third], vals[third:2 * third], vals[2 * third:]]
    out = []
    for g in groups:
        ys = [y for _, y in g]
        wr = sum(1 for _, y in g if y > 0) / len(g) * 100
        out.append((statistics.mean(x for x, _ in g), statistics.mean(ys), wr, len(g)))
    return out


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*104}")
    print(f"  ESTUDIO G — ¿COMPRAR LA CIMA? extensión + desaceleración | Marea BASE | top-80 | desde 2020 (bear)")
    print(f"{'#'*104}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    feats = regime_features(qqq, data, cal)
    hyst = expo_series(cal, feats, "hysteresis", {"band": 0.03})

    base = run_marea(data, cal, hyst, capture=True)
    ents = base["entries"]
    done = [e for e in ents if e.get("realized") is not None]
    print(f"  {len(ents)} entradas capturadas ({len(done)} ya cerradas con P&L)\n")

    # ── 1) ¿predicen? correlación métrica-entrada vs resultado forward ──
    metrics = [("ext_atr", "ATRs sobre breakout"), ("pct_ema20", "% sobre EMA20"), ("rsi14", "RSI14"),
               ("accel21", "accel mom21 (<0=desacelera)"), ("m21_vs_pace", "21d vs ritmo 63d (<0=lento)")]
    print(f"  ── 1) ¿La señal en la ENTRADA predice el resultado? (correlación; ~0 = no predice) ──")
    print(f"  {'Métrica':<30} {'corr→fwd20':>11} {'corr→realiz':>12} {'corr→MAE':>10}")
    print(f"  {'-'*64}")
    for mk, lbl in metrics:
        c1 = corr([e[mk] for e in ents], [e["fwd20"] for e in ents])
        c2 = corr([e[mk] for e in done], [e["realized"] for e in done])
        c3 = corr([e[mk] for e in done], [e["mae"] for e in done])
        print(f"  {lbl:<30} {c1:>+11.2f} {c2:>+12.2f} {c3:>+10.2f}")

    # ── 2) terciles: ¿las entradas MÁS extendidas/desaceleradas rinden peor? ──
    print(f"\n  ── 2) TERCILES por señal → resultado (¿el tercil ALTO rinde peor?) ──")
    for mk, lbl in metrics:
        t = terciles(done, mk, "realized")
        if not t: continue
        print(f"  {lbl}:")
        for name, (mv, ry, wr, n) in zip(["bajo ", "medio", "ALTO "], t):
            print(f"      {name}  {mk}~{mv:>+7.2f}  realized medio {ry:>+6.2f}%  WR {wr:>4.0f}%  (n={n})")

    # ── 3) ¿FILTRAR ayuda? variantes que saltan entradas según cada señal ──
    print(f"\n  ── 3) ¿Filtrar la entrada MEJORA la cartera? (vs BASE) ──")
    fl = [
        ("BASE (sin filtro)",            None),
        ("skip ext_atr>4 (muy estirado)", lambda m: m["ext_atr"] is not None and m["ext_atr"] > 4),
        ("skip ext_atr>6",                lambda m: m["ext_atr"] is not None and m["ext_atr"] > 6),
        ("skip RSI14>80",                 lambda m: m["rsi14"] is not None and m["rsi14"] > 80),
        ("skip pct_ema20>15%",            lambda m: m["pct_ema20"] is not None and m["pct_ema20"] > 15),
        ("skip accel21<0 (desacelera)",   lambda m: m["accel21"] is not None and m["accel21"] < 0),
        ("skip m21_vs_pace<0 (ritmo↓)",   lambda m: m["m21_vs_pace"] is not None and m["m21_vs_pace"] < 0),
    ]
    print(f"  {'Variante':<30} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'WR%':>5} {'Trades':>6} {'%Inv':>5}")
    print(f"  {'-'*68}")
    for name, skip in fl:
        r = run_marea(data, cal, hyst, skip=skip); s = summarize(r)
        print(f"  {name:<30} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['wr']:>4.0f}% "
              f"{s['trades']:>6} {s['invested']:>4.0f}%")

    print(f"\n{'='*104}")
    print("  Lectura: si las correlaciones son ~0 y los terciles ALTO no rinden peor → la señal NO predice")
    print("  el techo (comprar 'la cima' no cuesta de forma sistemática; el chandelier ya lo gestiona). Si")
    print("  algún filtro SUBE Sharpe sin bajar CAGR a la par → hay edge real. Prior: probablemente no.")
    print(f"{'='*104}\n")


if __name__ == "__main__":
    main()
