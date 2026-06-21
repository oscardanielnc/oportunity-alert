#!/usr/bin/env python3
"""
EARNINGS SHAPE — perfil intradía por-ticker de la reacción a earnings (¿el patrón se repite?).

Por cada uno de los últimos ~12 trimestres de un ticker baja:
  - velas 1m de [report-1h, report+2h]  (la forma inmediata: spike / sell-the-news / recuperación)
  - velas 1h de los ~12 días siguientes (el drift posterior)
Normaliza TODO a % desde el precio al momento del reporte (la varianza, no el precio absoluto), de
modo que las curvas de distintos trimestres se puedan SUPERPONER. Etiqueta cada trimestre con
EPS estimate vs actual (sorpresa) y un veredicto de precio (bueno/malo/neutro por la reacción).

Output:
  - consola: por ticker, nº de trimestres con datos, score de REPETIBILIDAD (correlación media de
    curvas dentro de cada bucket bueno/malo) y la lista numerada de trimestres.
  - data/earnings_shape.html: gráfico LOCAL interactivo — eliges ticker y marcás qué trimestres
    superponer; dos charts (1m intradía / 1h post-días) en %, con leyenda numerada.

Datos: Alpaca SIP (1m/1h, ya lo usamos; incluye extended-hours) + yfinance (fechas/EPS). Sin pagar.
Uso: python -m research.earnings_shape [TICKER...]   (default: MU APP NVDA AVGO PLTR)
"""
import os, sys, json, time, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all


def finnhub_revenue_map(ticker, from_date):
    """{fecha: revenue_surprise%} desde Finnhub /calendar/earnings (revenueActual vs revenueEstimate).
    Gratis con la key que ya usamos. Falla a {} si no hay key/datos."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key or not from_date:
        return {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {}
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": from_date, "to": today, "symbol": ticker, "token": key}, timeout=20)
        if r.status_code == 200:
            for e in r.json().get("earningsCalendar", []):
                d = e.get("date"); ra = e.get("revenueActual"); re_ = e.get("revenueEstimate")
                if d and ra is not None and re_ not in (None, 0):
                    out[d] = round((ra - re_) / abs(re_) * 100, 2)
    except Exception:
        pass
    return out

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_HTML = os.path.join(_ROOT, "data", "earnings_shape.html")
N_QUARTERS = 12
PRE_MIN, POST_MIN = 60, 120        # ventana 1m: -1h .. +2h del reporte
POST_DAYS = 12                     # ventana 1h: días siguientes
GOOD_THR = 2.0                     # |reacción día1| para veredicto bueno/malo (si no, neutro)
DEFAULT_TICKERS = ["MU", "APP", "NVDA", "AVGO", "PLTR"]


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def earnings_dates(ticker):
    """[(report_dt_utc, hour 'amc'|'bmo', eps_est, eps_actual, surprise_pct)] últimos N trimestres."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).get_earnings_dates(limit=N_QUARTERS + 6)
    except Exception as e:
        print(f"  [{ticker}] yfinance error: {e}"); return []
    if df is None or df.empty:
        return []
    today = datetime.now(timezone.utc)
    out = []
    for idx in df.index:
        try:
            dt = idx.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
        except Exception:
            continue
        if dt_utc > today:
            continue
        row = df.loc[idx]
        est = row.get("EPS Estimate"); act = row.get("Reported EPS"); sur = row.get("Surprise(%)")
        def _f(x):
            try:
                x = float(x); return None if x != x else x   # NaN check
            except Exception:
                return None
        est, act, sur = _f(est), _f(act), _f(sur)
        if act is None:        # earnings futuro/sin reportar
            continue
        # hora ET para amc/bmo (idx viene en ET); aprox por la hora local del timestamp
        hour = "bmo" if idx.hour < 12 else "amc"
        out.append((dt_utc, hour, est, act, sur))
    out.sort(key=lambda x: x[0])
    return out[-N_QUARTERS:]


def daily_map(ticker, start_dt):
    bars = fetch_all(ticker, "1Day", _iso(start_dt))
    return {b["t"][:10]: b for b in bars}, [b["t"][:10] for b in bars]


def reaction(dmap, ddates, report_dt, hour):
    """gap y reacción día1 (close-to-close) por barras diarias, igual semántica que PED."""
    d = report_dt.strftime("%Y-%m-%d")
    if d not in dmap:
        # buscar el día hábil >= fecha del reporte
        later = [x for x in ddates if x >= d]
        if not later:
            return None, None
        d = later[0]
    i = ddates.index(d)
    r = i if hour == "bmo" else i + 1     # día de reacción
    if r <= 0 or r >= len(ddates):
        return None, None
    pre = dmap[ddates[r-1]]["c"]; rb = dmap[ddates[r]]
    if pre <= 0:
        return None, None
    return (rb["o"]-pre)/pre*100, (rb["c"]-pre)/pre*100   # gap, react


def _cache_path(ticker):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_shape_{ticker}.json")


def build_ticker(ticker):
    # Cache de la parte CARA (fetch). --fresh para reconstruir. Lo derivado (repetibilidad,
    # promedios, clasificador, diagnóstico) se recalcula siempre (barato) → iterar sin re-bajar.
    cp = _cache_path(ticker)
    if os.path.exists(cp) and "--fresh" not in sys.argv:
        cached = json.load(open(cp, encoding="utf-8"))
        events, nom = cached["events"], cached["nom"]
        print(f"  [{ticker}] {len(events)} trimestres (cache)")
    else:
        events, nom = _fetch_events(ticker)
        if not events:
            return None
        json.dump({"events": events, "nom": nom}, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    # Revenue surprise: NO disponible gratis (Finnhub free /calendar/earnings no da histórico;
    # /stock/earnings es solo EPS). Queda como campo MANUAL en la card (rev). finnhub_revenue_map
    # queda listo por si algún día se conecta una fuente con revenue (FMP/Benzinga).
    for e in events:
        e["rev_surprise"] = None
    # Clasificación de REACCIÓN AFTER-HOURS (forma del 1m) + head-fake (AH no coincide con resultado).
    for e in events:
        ys = [y for x, y in e["m1"] if 90 <= x <= 120]
        if not ys and e["m1"]:
            ys = [e["m1"][-1][1]]
        am = sum(ys)/len(ys) if ys else 0.0
        e["ah_move"] = round(am, 2)
        e["ah"] = "pop" if am >= 0.5 else "drop" if am <= -0.5 else "flat"
        r = e.get("react")
        e["headfake"] = bool((e["ah"] == "pop" and r is not None and r < 0) or
                             (e["ah"] == "drop" and r is not None and r > 0))
    # Dos esquemas de clasificación: por RESULTADO (verdict) y por FORMA AH (ah).
    SCH = {"resultado": ("verdict", "bueno", "malo"), "ah": ("ah", "pop", "drop")}
    repeat, diag = {}, {}
    for name, (fld, pos, neg) in SCH.items():
        repeat[name] = {"pos": _repeatability(events, fld, pos), "neg": _repeatability(events, fld, neg)}
        apos = {"m1": _avg_curve(events, fld, pos, "m1", 2), "h1": _avg_curve(events, fld, pos, "h1", 2)}
        aneg = {"m1": _avg_curve(events, fld, neg, "m1", 2), "h1": _avg_curve(events, fld, neg, "h1", 2)}
        diag[name] = {"pos": _diagnose(apos, nom), "neg": _diagnose(aneg, nom)}
    gapcls = _gap_classifier(events)
    return {"events": events, "repeat": repeat, "nom": nom, "gapcls": gapcls, "diag": diag}


def _fetch_events(ticker):
    evs = earnings_dates(ticker)
    if not evs:
        print(f"  [{ticker}] sin earnings."); return [], 960
    dmap, ddates = daily_map(ticker, evs[0][0] - timedelta(days=5))
    events = []
    for k, (t0, hour, est, act, sur) in enumerate(evs):
        m1 = fetch_all(ticker, "1Min", _iso(t0 - timedelta(minutes=PRE_MIN)),
                       _iso(t0 + timedelta(minutes=POST_MIN)))
        if not m1:
            print(f"  [{ticker}] {t0.date()} sin 1m (histórico fuera de rango) — saltado"); continue
        # precio de referencia = barra 1m más cercana al reporte
        ref_bar = min(m1, key=lambda b: abs((_parse(b["t"]) - t0).total_seconds()))
        ref = ref_bar["c"]
        if ref <= 0:
            continue
        m1pts = [[round((_parse(b["t"]) - t0).total_seconds()/60, 1), round((b["c"]/ref-1)*100, 3)]
                 for b in m1]
        h1 = fetch_all(ticker, "1Hour", _iso(t0), _iso(t0 + timedelta(days=POST_DAYS)))
        h1pts = [[round((_parse(b["t"]) - t0).total_seconds()/3600, 2), round((b["c"]/ref-1)*100, 3)]
                 for b in h1]
        gap, react = reaction(dmap, ddates, t0, hour)
        verdict = "neutro"
        if react is not None:
            verdict = "bueno" if react >= GOOD_THR else "malo" if react <= -GOOD_THR else "neutro"
        events.append({
            "date": t0.strftime("%Y-%m-%d"), "hour": hour,
            "est": est, "act": act, "surprise": sur,
            "gap": None if gap is None else round(gap, 2),
            "react": None if react is None else round(react, 2),
            "verdict": verdict, "m1": m1pts, "h1": h1pts,
        })
        time.sleep(0.1)
    if not events:
        print(f"  [{ticker}] ningún trimestre con 1m disponible."); return [], 960
    amc = sum(1 for e in events if e["hour"] == "amc")
    nom = 960 if amc >= len(events) / 2 else 570   # hora ET nominal del reporte: 16:00 amc / 09:30 bmo
    return events, nom


def _repeatability(events, field, value):
    """Correlación media por pares de las curvas 1m (minutos compartidos) dentro del bucket field==value."""
    curves = [{round(x): y for x, y in e["m1"]} for e in events if e.get(field) == value]
    if len(curves) < 2:
        return None
    cors, npairs = [], 0
    for i in range(len(curves)):
        for j in range(i+1, len(curves)):
            common = sorted(set(curves[i]) & set(curves[j]))
            if len(common) < 20:
                continue
            a = [curves[i][m] for m in common]; b = [curves[j][m] for m in common]
            c = _pearson(a, b)
            if c is not None:
                cors.append(c); npairs += 1
    if not cors:
        return None
    return {"corr": round(sum(cors)/len(cors), 2), "n": len([e for e in events if e.get(field)==value]),
            "pairs": npairs}


def _pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a)/n, sum(b)/n
    va = sum((x-ma)**2 for x in a); vb = sum((x-mb)**2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    return cov/(va**0.5 * vb**0.5)


def _avg_curve(events, field, value, key, step):
    """Curva PROMEDIO del bucket (field==value): agrupa por x (redondeado a step) y promedia."""
    bucket = {}
    for e in events:
        if e.get(field) != value:
            continue
        for x, y in e[key]:
            g = round(x / step) * step
            bucket.setdefault(g, []).append(y)
    return [[g, round(sum(v)/len(v), 3)] for g, v in sorted(bucket.items()) if len(v) >= 2]


def _gap_classifier(events):
    """¿El GAP (conocido al abrir) predice la reacción del día? Acierto de signo, correlación,
    precisión de la regla gap>=+2→bueno / gap<=-2→malo, y el 'fade' medio (reac-gap) por bucket."""
    P = [(e["gap"], e["react"]) for e in events if e["gap"] is not None and e["react"] is not None]
    n = len(P)
    if n < 3:
        return None
    THR = 2.0
    sign = sum(1 for g, r in P if (g > 0) == (r > 0)) / n
    corr = _pearson([g for g, _ in P], [r for _, r in P])
    pb = [(g, r) for g, r in P if g >= THR]
    pm = [(g, r) for g, r in P if g <= -THR]
    def _mean(xs): return round(sum(xs)/len(xs), 2) if xs else None
    fb = _mean([r - g for g, r in P if r >= THR])   # fade en días buenos (reac vs gap)
    return {
        "n": n, "sign_agree": round(100*sign), "corr": round(corr, 2) if corr is not None else None,
        "rule_thr": THR,
        "bueno_prec": round(100*sum(1 for g, r in pb if r >= THR)/len(pb)) if pb else None,
        "bueno_cov": len(pb),
        "malo_prec": round(100*sum(1 for g, r in pm if r <= -THR)/len(pm)) if pm else None,
        "malo_cov": len(pm),
        "fade_bueno": fb,   # negativo = sell-the-news (cierra por debajo del gap)
    }


def _diagnose(avg, nom):
    """Horas clave de la FORMA PROMEDIO (para guiar entrada/salida). Del 1h: gap, dip (sell-the-news)
    y pico de recuperación. Del 1m: pico inmediato after-hours. Tiempos relativos al reporte."""
    m1, h1 = avg.get("m1") or [], avg.get("h1") or []
    d = {"has": bool(h1 or m1)}
    if h1:
        post = [p for p in h1 if p[0] >= 15]   # tras el gap de apertura del día de reacción
        seg = post or h1
        dip = min(seg, key=lambda p: p[1])
        after = [p for p in seg if p[0] > dip[0]] or [dip]
        peak = max(after, key=lambda p: p[1])
        gap_lvl = max((p[1] for p in h1 if p[0] <= 20), default=h1[-1][1])
        d.update({"gap_lvl": round(gap_lvl, 1),
                  "dip_h": round(dip[0], 1), "dip_lvl": round(dip[1], 1),
                  "peak_h": round(peak[0], 1), "peak_lvl": round(peak[1], 1),
                  "end_lvl": round(h1[-1][1], 1)})
    if m1:
        ah = max(m1, key=lambda p: p[1]); al = min(m1, key=lambda p: p[1])
        d.update({"ah_peak_min": round(ah[0]), "ah_peak_lvl": round(ah[1], 1),
                  "ah_low_min": round(al[0]), "ah_low_lvl": round(al[1], 1)})
    return d


def main():
    tickers = [t.upper() for t in sys.argv[1:]] or DEFAULT_TICKERS
    print(f"\n=== EARNINGS SHAPE — {', '.join(tickers)} (últimos {N_QUARTERS} trimestres) ===")
    data = {}
    for tk in tickers:
        print(f"\n[{tk}]")
        d = build_ticker(tk)
        if not d:
            continue
        data[tk] = d
        rr, ra = d["repeat"]["resultado"], d["repeat"]["ah"]
        rb, rm = rr["pos"], rr["neg"]; ap, an = ra["pos"], ra["neg"]
        print(f"  trimestres con 1m: {len(d['events'])}")
        print(f"  REPETIBILIDAD 1m  [resultado] bueno: {rb['corr'] if rb else '—'} (n={rb['n'] if rb else 0})"
              f"  malo: {rm['corr'] if rm else '—'} (n={rm['n'] if rm else 0})  |  "
              f"[forma AH] pop: {ap['corr'] if ap else '—'} (n={ap['n'] if ap else 0})"
              f"  drop: {an['corr'] if an else '—'} (n={an['n'] if an else 0})")
        gc = d.get("gapcls")
        if gc:
            print(f"  CLASIFICADOR x GAP: acierto de signo {gc['sign_agree']}% | corr(gap,reac) {gc['corr']} | "
                  f"regla gap>=+{gc['rule_thr']:.0f}->bueno prec {gc['bueno_prec']}% (n={gc['bueno_cov']}), "
                  f"gap<=-{gc['rule_thr']:.0f}->malo prec {gc['malo_prec']}% (n={gc['malo_cov']}) | "
                  f"fade buenos {gc['fade_bueno']}% (neg=sell-the-news)")
        db = d["diag"]["resultado"]["pos"]
        if db.get("has") and "dip_h" in db:
            print(f"  FORMA BUENA (promedio): gap ~+{db['gap_lvl']}% -> dip ~+{db['dip_lvl']}% a +{db['dip_h']}h "
                  f"(ENTRADA) -> pico ~+{db['peak_lvl']}% a +{db['peak_h']}h (SALIDA) -> cierra ~+{db['end_lvl']}%")
        print(f"  {'#':<3}{'fecha':<12}{'gap%':>7}{'reac%':>7}{'AH%':>7}  {'resultado':<9}{'AH':<6}headfake")
        for i, e in enumerate(d["events"]):
            print(f"  {i:<3}{e['date']:<12}{str(e['gap']):>7}{str(e['react']):>7}{str(e.get('ah_move')):>7}  "
                  f"{e['verdict']:<9}{e.get('ah',''):<6}{'  <== HEAD-FAKE' if e.get('headfake') else ''}")
    if not data:
        print("\nSin datos. ¿Alpaca/yfinance OK?"); return
    _write_html(data)
    print(f"\nGráfico interactivo -> {OUT_HTML}  (ábrelo en el navegador)")


def _write_html(data):
    html = _HTML_TMPL.replace("/*DATA*/", json.dumps(data, ensure_ascii=False))
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


_HTML_TMPL = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Earnings Shape</title><style>
*{box-sizing:border-box} html,body{height:100%;margin:0}
body{font-family:system-ui,Arial,sans-serif;background:#0f1117;color:#e6e6e6}
#app{display:flex;flex-direction:column;height:100vh}
header{flex:0 0 auto;display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:8px 14px;background:#161a23;border-bottom:1px solid #262b36}
header h1{font-size:15px;margin:0}
select{background:#1c2230;color:#e6e6e6;border:1px solid #333c4d;border-radius:6px;padding:5px 8px;font-size:14px}
.rep{font-size:12px;color:#aab}
.seg button.on{background:#3257a8;color:#fff;border-color:#3257a8}
button{background:#222a38;color:#cbd3e1;border:1px solid #333c4d;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
#chartbox{flex:1 1 auto;min-height:0;position:relative;padding:6px 10px}
#cv{width:100%;height:100%;display:block;background:#12151d;border:1px solid #232834;border-radius:8px}
#filters{flex:0 0 auto;height:27vh;overflow:hidden;border-top:1px solid #262b36;background:#13161e;padding:8px 12px;display:flex;gap:12px}
#groups{flex:0 0 300px;display:flex;flex-direction:column;min-height:0;border-right:1px solid #232a37;padding-right:10px}
.ghead{font-size:12px;font-weight:700;color:#9fb0d6;margin-bottom:6px}
#glist{flex:1;overflow:auto;display:flex;flex-direction:column;gap:6px;margin-bottom:8px}
.gcard{background:#1a2030;border:1px solid #2a3344;border-left:3px solid #6ea8fe;border-radius:8px;padding:6px 8px;cursor:pointer;font-size:12px}
.gcard:hover{background:#222b3d}
.gtitle{display:flex;align-items:center;gap:6px;font-weight:700}
.gtitle .gmode{font-size:10px;background:#2a3550;color:#9fb0d6;border-radius:4px;padding:1px 6px;font-weight:600}
.gtitle .gx{margin-left:auto;color:#f87171;font-weight:800;padding:0 4px;cursor:pointer}
.gitems{color:#cbd3e1;margin-top:2px}
.gnote{color:#8a93a6;margin-top:2px;white-space:pre-wrap}
.gform{display:flex;flex-direction:column;gap:4px}
.gform input,.gform select,.gform textarea{background:#1c2230;color:#e6e6e6;border:1px solid #333c4d;border-radius:5px;padding:4px 6px;font-size:12px;font-family:inherit}
.gform textarea{resize:vertical;min-height:30px}
#chips{flex:1;min-width:0;overflow:auto;display:flex;flex-wrap:wrap;align-content:flex-start;gap:8px}
.tools{display:flex;gap:6px;flex-wrap:wrap;width:100%;margin-bottom:4px}
.qcard{background:#1a2030;border:1px solid #2a3344;border-radius:8px;padding:6px 8px;font-size:12px;width:158px;flex:0 0 auto}
.qcard.foc{outline:2px solid #6ea8fe}
.qhead{display:flex;align-items:center;gap:5px;cursor:pointer;font-weight:600;margin-bottom:3px}
.qhead .sw{width:14px;height:4px;border-radius:2px}
.qhead .qe{margin-left:auto;color:#9fb0d6;cursor:pointer}
.qrow{display:flex;justify-content:space-between;gap:6px;line-height:1.55}
.qcard input:not([type=checkbox]){width:88px;background:#12151d;color:#e6e6e6;border:1px solid #333c4d;border-radius:4px;padding:1px 4px;font-size:11px}
.switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-size:12px;color:#cbd3e1;user-select:none}
.switch input{display:none}
.track{width:38px;height:20px;background:#39414f;border-radius:11px;position:relative;transition:.15s}
.knob{position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#cbd3e1;transition:.15s}
.switch input:checked + .track{background:#3257a8}
.switch input:checked + .track .knob{left:20px;background:#fff}
.bueno{color:#4ade80}.malo{color:#f87171}.neutro{color:#cbb761}
.muted{color:#8a93a6}
#diag{flex:0 0 auto;padding:6px 12px;background:#10131b;border-bottom:1px solid #1e2430;display:flex;gap:8px;align-items:center;overflow-x:auto;white-space:nowrap}
.gchip{flex:0 0 auto;background:#1a2030;border:1px solid #2a3344;border-left:3px solid #6ea8fe;border-radius:8px;padding:4px 9px;cursor:pointer;font-size:12px;display:inline-flex;align-items:center;gap:6px}
.gchip:hover{background:#222b3d}
.gchip.foc{outline:2px solid #6ea8fe}
.gchip .gm{font-size:10px;background:#2a3550;color:#9fb0d6;border-radius:4px;padding:1px 5px}
.gchip .gi{color:#8a93a6}
.gchip .ge{color:#9fb0d6;cursor:pointer}
.gchip .gd{color:#f87171;font-weight:800;cursor:pointer}
</style></head><body>
<div id="app">
 <header>
  <h1>📊 Earnings Shape</h1>
  <select id="tk" onchange="pick()"></select>
  <span id="rep" class="rep"></span>
 </header>
 <div id="diag"></div>
 <div id="chartbox"><canvas id="cv"></canvas></div>
 <div id="filters">
   <div id="groups">
     <div class="ghead">➕ Crear / editar grupo <span class="muted" style="font-weight:400">(se guardan en este navegador)</span></div>
     <div class="gform">
       <input id="gname" placeholder="nombre del grupo (ej. pops fuertes)">
       <select id="gmode" onchange="setMode(this.value)"><option value="m1">1m intradía</option><option value="h1">1h post-días</option></select>
       <textarea id="gnote" placeholder="por qué agrupo estos trimestres así…"></textarea>
       <div style="display:flex;gap:4px">
         <button id="gsave" onclick="saveGroup()" style="flex:1">+ Guardar grupo (selección actual)</button>
         <button id="gcancel" onclick="cancelEditGroup()" style="display:none">cancelar</button>
       </div>
     </div>
   </div>
   <div id="chips">
     <div class="tools">
       <button onclick="allSel(1)">Todos</button><button onclick="allSel(0)">Ninguno</button>
       <label class="switch"><input type="checkbox" id="bn" onchange="toggleNorm()"><span class="track"><span class="knob"></span></span>Misma amplitud</label>
     </div>
   </div>
 </div>
</div>
<script>
const DATA = /*DATA*/;
const COLORS=["#60a5fa","#f87171","#4ade80","#fbbf24","#c084fc","#22d3ee","#fb923c","#a3e635","#f472b6","#94a3b8","#38bdf8","#e879f9"];
let TK=Object.keys(DATA)[0], MODE='m1', shown={}, focus=null, _geo=null, normAmp=false;
const tksel=document.getElementById('tk');
function predLabel(d){const r=d.repeat.resultado||{};const c=Math.max((r.pos&&r.pos.corr)??-9,(r.neg&&r.neg.corr)??-9);
  if(c<-1)return['—','#8a93a6']; return c>=0.6?['ALTA','#4ade80']:c>=0.3?['MEDIA','#fbbf24']:['BAJA','#f87171'];}
function fillOptions(){[...tksel.options].forEach(o=>o.textContent=o.value+' · '+predLabel(DATA[o.value])[0]);}
Object.keys(DATA).forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t;tksel.appendChild(o);});
function setMode(m){MODE=m;const s=document.getElementById('gmode');if(s)s.value=m;draw();}
function pick(){TK=tksel.value;shown={};focus=null;normAmp=false;document.getElementById('bn').checked=false;DATA[TK].events.forEach((e,i)=>shown[i]=true);render();}
function allSel(on){DATA[TK].events.forEach((e,i)=>{shown[i]=!!on;});render();}
function toggleNorm(){normAmp=document.getElementById('bn').checked;draw();}
function avgOf(events,key,step){const b={};events.forEach(e=>e[key].forEach(p=>{const g=Math.round(p[0]/step)*step;(b[g]=b[g]||[]).push(p[1]);}));
  return Object.keys(b).map(Number).sort((a,c)=>a-c).filter(g=>b[g].length>=2).map(g=>[g,b[g].reduce((s,v)=>s+v,0)/b[g].length]);}
function pad(n){return String(n).padStart(2,'0')}
function clock(min){let t=((Math.round(min))%1440+1440)%1440;return pad(Math.floor(t/60))+':'+pad(t%60)}
function xlabel(v){ if(MODE==='m1'){return clock(DATA[TK].nom+v)+' ET'} return Math.abs(v)<24?('+'+Math.round(v)+'h'):('+'+(v/24).toFixed(1)+'d'); }
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
// ── Grupos personalizados (persistidos por ticker en localStorage) ───────────────
const GKEY='earnings_groups_v1', NKEY='earnings_notes_v1';
function loadLS(k){try{return JSON.parse(localStorage.getItem(k)||'{}');}catch(e){return {};}}
function persistGroups(){try{localStorage.setItem(GKEY,JSON.stringify(GROUPS));}catch(e){}}
function persistNotes(){try{localStorage.setItem(NKEY,JSON.stringify(NOTES));}catch(e){}}
let GROUPS=loadLS(GKEY), NOTES=loadLS(NKEY), editGroupIdx=null, editNote=null;
function getNote(date){return ((NOTES[TK]||{})[date])||{};}
function saveNote(date){const rev=document.getElementById('erev').value.trim(),guid=document.getElementById('eguid').value.trim();
  (NOTES[TK]=NOTES[TK]||{})[date]={rev,guid};persistNotes();editNote=null;render();}
function cancelNote(){editNote=null;render();}
function revColor(s){const n=parseFloat(String(s||'').replace('%',''));if(isNaN(n))return'neutro';return n>=0?'bueno':'malo';}
function guidColor(s){s=(s||'').toLowerCase();if(/déb|debil|weak|baj|flo|recort|reduc/.test(s))return'malo';if(/fuert|strong|sólid|solid|sube|alc|eleva|robust/.test(s))return'bueno';return'neutro';}
function renderGroups(){
  const L=document.getElementById('diag');L.innerHTML='';
  const arr=GROUPS[TK]||[];
  if(!arr.length){L.innerHTML='<span class="muted" style="font-size:12px">Sin grupos guardados — creá uno abajo a la izquierda ↙</span>';return;}
  arr.forEach((g,idx)=>{
    const c=document.createElement('span');c.className='gchip'+(editGroupIdx===idx?' foc':'');c.onclick=()=>applyGroup(idx);
    c.title=g.note||'';
    c.innerHTML=`<b>${esc(g.name||'(sin nombre)')}</b> <span class="gm">${g.mode==='h1'?'1h':'1m'}</span> <span class="gi">${(g.items||[]).join(',')}</span>`+
      `<span class="ge" title="editar">✎</span><span class="gd" title="borrar">×</span>`;
    c.querySelector('.ge').onclick=ev=>{ev.stopPropagation();editGroup(idx);};
    c.querySelector('.gd').onclick=ev=>{ev.stopPropagation();if(confirm('¿Borrar grupo "'+(g.name||'')+'"?')){GROUPS[TK].splice(idx,1);persistGroups();if(editGroupIdx===idx)cancelEditGroup();else renderGroups();}};
    L.appendChild(c);
  });
}
function editGroup(idx){
  const g=(GROUPS[TK]||[])[idx];if(!g)return;
  editGroupIdx=idx;
  document.getElementById('gname').value=g.name||'';
  document.getElementById('gmode').value=g.mode||'m1';
  document.getElementById('gnote').value=g.note||'';
  document.getElementById('gsave').textContent='✓ Actualizar grupo';
  document.getElementById('gcancel').style.display='';
  applyGroup(idx);
}
function cancelEditGroup(){
  editGroupIdx=null;
  document.getElementById('gname').value='';document.getElementById('gnote').value='';
  document.getElementById('gsave').textContent='+ Guardar grupo (selección actual)';
  document.getElementById('gcancel').style.display='none';
  renderGroups();
}
function saveGroup(){
  const name=document.getElementById('gname').value.trim();
  const mode=document.getElementById('gmode').value;
  const note=document.getElementById('gnote').value.trim();
  const items=DATA[TK].events.map((e,i)=>i).filter(i=>shown[i]);
  if(!items.length){alert('Marcá al menos un trimestre antes de guardar.');return;}
  if(!name){alert('Ponle un nombre al grupo.');return;}
  GROUPS[TK]=GROUPS[TK]||[];
  if(editGroupIdx!=null) GROUPS[TK][editGroupIdx]={name,mode,note,items};
  else GROUPS[TK].push({name,mode,note,items});
  persistGroups();
  cancelEditGroup();
}
function applyGroup(idx){
  const g=(GROUPS[TK]||[])[idx];if(!g)return;
  shown={};DATA[TK].events.forEach((e,i)=>shown[i]=false);
  (g.items||[]).forEach(i=>{if(i<DATA[TK].events.length)shown[i]=true;});
  focus=null;setMode(g.mode||'m1');render();
}
function render(){
  const d=DATA[TK], R=d.repeat.resultado||{};
  const rb=R.pos, rm=R.neg, pl=predLabel(d);
  document.getElementById('rep').innerHTML=`<span style="background:${pl[1]}22;color:${pl[1]};border:1px solid ${pl[1]}66;border-radius:6px;padding:2px 8px;font-weight:800">${pl[0]} predecibilidad</span> &nbsp;Repetibilidad 1m — bueno: <b>${rb?rb.corr:'—'}</b> (n=${rb?rb.n:0}) · malo: <b>${rm?rm.corr:'—'}</b> (n=${rm?rm.n:0}) <span class="muted">(>0.6 consistente, &lt;0.3 ruido)</span>`;
  renderGroups();   // barra de grupos arriba (#diag)
  const f=document.getElementById('chips');
  f.querySelectorAll('.qcard').forEach(c=>c.remove());
  d.events.forEach((e,i)=>{
    const beat=(e.est!=null&&e.act!=null)?+(e.act-e.est).toFixed(2):null;
    const bc=beat==null?'neutro':(beat>=0?'bueno':'malo');
    const bs=beat==null?'—':`${beat>=0?'+':''}${beat}${e.surprise!=null?` (${e.surprise>=0?'+':''}${e.surprise}%)`:''}`;
    const nt=getNote(e.date);
    const card=document.createElement('div');card.className='qcard'+(focus===i?' foc':'');
    if(editNote===i){
      card.innerHTML=`<div class="qhead"><b>#${i}</b> ${e.date} — editar</div>`+
        `<div class="qrow"><span class="muted">Rev surprise</span><input id="erev" value="${esc(nt.rev||'')}" placeholder="ej. +2.5%"></div>`+
        `<div class="qrow"><span class="muted">Guidance</span><input id="eguid" value="${esc(nt.guid||'')}" placeholder="débil / fuerte / en línea"></div>`+
        `<div class="qrow" style="justify-content:flex-end;gap:4px"><button onclick="saveNote('${esc(e.date)}')">✓</button><button onclick="cancelNote()">✗</button></div>`;
    } else {
      card.innerHTML=`<label class="qhead"><input type="checkbox" ${shown[i]?'checked':''}><span class="sw" style="background:${COLORS[i%COLORS.length]}"></span><b>#${i}</b> ${e.date}<span class="qe" title="editar Rev/Guidance">✎</span></label>`+
        `<div class="qrow"><span class="muted">EPS</span><b class="${bc}">${bs}</b></div>`+
        `<div class="qrow"><span class="muted">Rev</span>${(e.rev_surprise!=null)?`<b class="${e.rev_surprise>=0?'bueno':'malo'}">${e.rev_surprise>=0?'+':''}${e.rev_surprise}%</b>`:`<span class="${revColor(nt.rev)}">${nt.rev?esc(nt.rev):'—'}</span>`}</div>`+
        `<div class="qrow"><span class="muted">Guid</span><span class="${guidColor(nt.guid)}">${nt.guid?esc(nt.guid):'—'}</span></div>`;
      card.querySelector('input[type=checkbox]').onchange=ev=>{ev.stopPropagation();shown[i]=ev.target.checked;if(focus===i&&!ev.target.checked)focus=null;draw();};
      card.querySelector('.qe').onclick=ev=>{ev.stopPropagation();editNote=i;render();};
      card.onclick=ev=>{const tg=ev.target.tagName;if(tg==='INPUT'||ev.target.classList.contains('qe'))return;focus=(focus===i?null:i);render();};
    }
    f.appendChild(card);
  });
  draw();
}
function draw(){
  const cv=document.getElementById('cv');const dpr=window.devicePixelRatio||1;
  const w=cv.clientWidth, h=cv.clientHeight;                  // CSS px (fix: no leer cv.height)
  cv.width=Math.max(1,Math.round(w*dpr));cv.height=Math.max(1,Math.round(h*dpr));
  const x=cv.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
  const d=DATA[TK], evs=d.events, key=MODE;
  const series=[];
  evs.forEach((e,i)=>{if(shown[i]&&e[key].length)series.push({pts:e[key],color:COLORS[i%COLORS.length],w:1.6,i:i});});
  // preview: si hay foco en un trimestre NO marcado, dibujarlo igual (sin marcarlo)
  if(focus!=null&&!shown[focus]&&evs[focus]&&evs[focus][key].length)
    series.push({pts:evs[focus][key],color:COLORS[focus%COLORS.length],w:1.6,i:focus});
  // PROMEDIO dinámico de lo seleccionado (siempre, sin importar bueno/malo/combinado)
  const shownEv=evs.filter((e,i)=>shown[i]&&e[key].length);
  const avg = shownEv.length>=2 ? avgOf(shownEv,key,2) : [];
  if(avg.length) series.push({pts:avg,color:'#f1f5ff',w:3,dash:[7,4],avg:true});
  x.font='11px system-ui';
  if(!series.length){x.fillStyle='#667';x.fillText('Marcá trimestres abajo (o activá una forma promedio)',20,30);return;}
  // "Misma amplitud": normaliza cada curva a 0–1 (min-max) para comparar forma/timing sin magnitud
  const SER = normAmp ? series.map(s=>{let lo=1e9,hi=-1e9;s.pts.forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1]);});const rng=(hi-lo)||1;
    return Object.assign({},s,{pts:s.pts.map(p=>[p[0],(p[1]-lo)/rng])});}) : series;
  let xmin=1e9,xmax=-1e9,ymin=1e9,ymax=-1e9;
  SER.forEach(s=>s.pts.forEach(p=>{xmin=Math.min(xmin,p[0]);xmax=Math.max(xmax,p[0]);ymin=Math.min(ymin,p[1]);ymax=Math.max(ymax,p[1]);}));
  if(!normAmp){if(ymin>0)ymin=0; if(ymax<0)ymax=0;} const padY=(ymax-ymin)*0.08||1; ymin-=padY;ymax+=padY;
  const L=52,R=14,T=12,B=26; const PX=v=>L+(v-xmin)/(xmax-xmin||1)*(w-L-R); const PY=v=>T+(ymax-v)/(ymax-ymin||1)*(h-T-B);
  const ysuf=normAmp?'':'%';
  x.strokeStyle='#222834';x.fillStyle='#8a93a6';x.lineWidth=1;
  for(let g=0;g<=5;g++){const yy=ymin+(ymax-ymin)*g/5;const py=PY(yy);x.beginPath();x.moveTo(L,py);x.lineTo(w-R,py);x.stroke();x.fillText(yy.toFixed(normAmp?2:1)+ysuf,6,py+3);}
  for(let g=0;g<=6;g++){const xv=xmin+(xmax-xmin)*g/6;const px=PX(xv);x.strokeStyle='#1c212c';x.beginPath();x.moveTo(px,T);x.lineTo(px,h-B);x.stroke();x.fillStyle='#8a93a6';x.fillText(xlabel(xv),px-16,h-9);}
  x.strokeStyle='#2c3340';const p0=PY(0);x.beginPath();x.moveTo(L,p0);x.lineTo(w-R,p0);x.stroke();
  if(0>=xmin&&0<=xmax){x.strokeStyle='#516084';x.beginPath();x.moveTo(PX(0),T);x.lineTo(PX(0),h-B);x.stroke();x.fillStyle='#9fb0d6';x.fillText('reporte',PX(0)+3,T+11);}
  // marcadores ENTRADA (mínimo) / SALIDA (máximo posterior) calculados sobre el PROMEDIO dinámico
  if(avg.length>2){
    const seg = key==='h1' ? avg.filter(p=>p[0]>=15) : avg; const base = seg.length?seg:avg;
    let dip=base[0]; base.forEach(p=>{if(p[1]<dip[1])dip=p;});
    let peak=dip; base.forEach(p=>{if(p[0]>dip[0]&&p[1]>peak[1])peak=p;});
    [[dip[0],'ENTRADA','#4ade80'],[peak[0],'SALIDA','#fbbf24']].forEach(([mx,lb,col])=>{if(mx==null||mx<xmin||mx>xmax)return;const px=PX(mx);
      x.strokeStyle=col;x.setLineDash([3,3]);x.beginPath();x.moveTo(px,T);x.lineTo(px,h-B);x.stroke();x.setLineDash([]);
      x.fillStyle=col;x.fillText(lb,px+3,T+24);});
  }
  SER.forEach(s=>{
    let a=1, lw=s.w;
    if(focus!=null){ if(s.i===focus){a=1;lw=2.8;} else {a=s.avg?0.22:0.13;} }
    x.globalAlpha=a;x.setLineDash(s.dash||[]);x.strokeStyle=s.color;x.lineWidth=lw;x.beginPath();
    s.pts.forEach((p,k)=>{const px=PX(p[0]),py=PY(p[1]);k?x.lineTo(px,py):x.moveTo(px,py);});x.stroke();});
  x.globalAlpha=1;x.setLineDash([]);
  _geo={xmin,xmax,ymin,ymax,L,R,T,B,w,h};
}
// Click en el canvas: resalta la línea más cercana (toggle); click en vacío limpia el foco.
document.getElementById('cv').addEventListener('click',ev=>{
  if(!_geo)return; const cv=ev.currentTarget; const r=cv.getBoundingClientRect();
  const mx=ev.clientX-r.left, my=ev.clientY-r.top, g=_geo;
  const px=v=>g.L+(v-g.xmin)/((g.xmax-g.xmin)||1)*(g.w-g.L-g.R);
  const py=v=>g.T+(g.ymax-v)/((g.ymax-g.ymin)||1)*(g.h-g.T-g.B);
  let best=null, bd=16;
  DATA[TK].events.forEach((e,i)=>{if((!shown[i]&&i!==focus)||!e[MODE].length)return;
    let lo=1e9,hi=-1e9; if(normAmp){e[MODE].forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1]);});}
    const rng=(hi-lo)||1;
    e[MODE].forEach(p=>{const yy=normAmp?(p[1]-lo)/rng:p[1];const dd=Math.hypot(px(p[0])-mx,py(yy)-my);if(dd<bd){bd=dd;best=i;}});});
  if(best!==null){focus=(focus===best?null:best);render();}
  else if(focus!=null){focus=null;render();}
});
fillOptions();setMode('m1');pick();
let _t;window.addEventListener('resize',()=>{clearTimeout(_t);_t=setTimeout(draw,80)});
</script></body></html>"""


if __name__ == "__main__":
    main()
