import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
VALIDACION — Pre-market scanner NETO de comisiones eToro (v2: con gates de calidad).

Mide si los GATES de calidad del scanner (RVOL, early-spike-fade, code_score>=6)
convierten la senal cruda en edge net-positivo sobre el grupo LARGE_CAP, e imprime
el detalle por senal para auditar outliers (ej. SHOP).

Reutiliza super_backtest.fetch_all (SIP) + backtest_perticker_v2.etoro_fee_analysis.
Senal tecnica = espejo de premarket_scanner. Capa IA NO modelada.
Entrada ~6am Lima (11:00 UTC). Salida = cierre regular mismo dia (20:00 UTC).
Sin spread/slippage.
"""
import os, sys
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_perticker_v2 import etoro_fee_analysis

GROUPS = {
    "LARGE_CAP": ["NVDA", "AMD", "MU", "AVGO", "MSFT", "PLTR", "GOOG",
                  "CRM", "ARM", "UBER", "SHOP", "TSM", "COHR"],
    "PUMP_SMALL": ["QBTS", "IONQ", "RGTI", "QUBT", "RDDT", "APP", "BBAI"],
}
TICKERS = GROUPS["LARGE_CAP"] + GROUPS["PUMP_SMALL"]
WEEKS   = 16

LONG_MIN_PCT, SHORT_MIN_PCT = 2.5, 3.5
MAX_RETRACE_PCT, MIN_CONSISTENCY, MIN_BARS = 1.5, 0.60, 6
PM_START_UTC_H, PM_END_UTC_H = 8, 11        # 3am-6am Lima
REG_OPEN_UTC, REG_CLOSE_UTC_H = (13, 30), 20
EARLY_SPIKE_UTC_H = 10                       # 5am Lima (early window 3-5am)
EARLY_FADE_PCT = 1.0
RVOL_FLOOR, RVOL_STRONG = 1.5, 3.0
PRICED_IN_PCT = 8.0
CODE_SCORE_MIN = 6


def _dt(b): return datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
def _lima_date(b): return (_dt(b) - timedelta(hours=5)).strftime("%Y-%m-%d")
def _weekday(b): return (_dt(b) - timedelta(hours=5)).weekday()

def _is_regular(b):
    m = _dt(b).hour * 60 + _dt(b).minute
    return (REG_OPEN_UTC[0]*60+REG_OPEN_UTC[1]) <= m <= REG_CLOSE_UTC_H*60

def _is_premarket(b): return PM_START_UTC_H <= _dt(b).hour < PM_END_UTC_H


def _consistency(bars, d):
    if not bars: return 0.0
    ok = sum(1 for b in bars if (d=="LONG" and b["c"]>=b["o"]) or (d=="SHORT" and b["c"]<=b["o"]))
    return ok/len(bars)

def _max_adverse(bars, d, w=6):
    worst = 0.0
    for i in range(max(0, len(bars)-w*3), len(bars)-w+1):
        win = bars[i:i+w]
        if len(win) < w: continue
        o = win[0]["o"]
        if o <= 0: continue
        adv = (o-min(b["l"] for b in win))/o*100 if d=="LONG" else (max(b["h"] for b in win)-o)/o*100
        worst = max(worst, max(0.0, adv))
    return worst

def _stabilization(bars, d):
    if len(bars) < 8: return 0
    rec, old = bars[-6:], (bars[-12:-6] if len(bars)>=12 else bars[:-6])
    if not old: return 0
    vr, vo = sum(b["v"] for b in rec), sum(b["v"] for b in old)
    vol_ok = vo > 0 and vr/vo >= 0.60
    rr = sum(b["h"]-b["l"] for b in rec)/len(rec)
    ro = sum(b["h"]-b["l"] for b in old)/len(old)
    range_ok = ro > 0 and rr < ro*0.90
    return int(vol_ok) + int(range_ok)


def _signal(pm, prev):
    if len(pm) < MIN_BARS or prev <= 0: return None
    entry = pm[-1]["c"]
    chg = (entry-prev)/prev*100
    d = "LONG" if chg >= LONG_MIN_PCT else "SHORT" if chg <= -SHORT_MIN_PCT else None
    if d is None: return None
    if _consistency(pm, d) < MIN_CONSISTENCY: return None
    if _max_adverse(pm, d) >= MAX_RETRACE_PCT: return None
    return d


def _early_fade(pm, d, entry):
    """Extremo formado antes de 5am Lima y ya retrocedio >=1%?"""
    if d == "LONG":
        idx = max(range(len(pm)), key=lambda i: pm[i]["h"]); ext = pm[idx]["h"]
    else:
        idx = min(range(len(pm)), key=lambda i: pm[i]["l"]); ext = pm[idx]["l"]
    ext_hour = _dt(pm[idx]).hour
    age_min = (_dt(pm[-1]) - _dt(pm[idx])).total_seconds()/60
    if ext_hour < EARLY_SPIKE_UTC_H:
        retr = max(0.0, (ext-entry)/ext*100) if d=="LONG" else max(0.0,(entry-ext)/abs(ext)*100)
        return (retr >= EARLY_FADE_PCT), age_min
    return False, age_min


def _code_score(pm, d, change_pct, pm_vol, rvol, entry, is_monday):
    fade, age = _early_fade(pm, d, entry)
    vol_s = 4 if pm_vol>=500_000 else 3 if pm_vol>=200_000 else 2 if pm_vol>=100_000 else 1 if pm_vol>=50_000 else 0
    rvol_s = 2 if rvol>=RVOL_STRONG else 1 if rvol>=RVOL_FLOOR else 0
    cons = _consistency(pm, d)
    mom_s = 3 if cons>=0.80 else 2 if cons>=0.70 else 1 if cons>=0.60 else 0
    time_s = 0 if fade else (2 if age<=30 else 1 if age<=60 else 0)
    day_s = 0 if is_monday else 1
    pat_s = _stabilization(pm, d)
    pen = 0
    if abs(change_pct) >= PRICED_IN_PCT: pen -= 1
    if fade: pen -= 2
    if entry < 5.0: pen -= 1
    total = max(0, vol_s+rvol_s+mom_s+time_s+day_s+pat_s+pen)
    return total, fade, rvol


def build_trades(ticker, bars):
    by_day = {}
    for b in bars: by_day.setdefault(_lima_date(b), []).append(b)
    days = sorted(by_day)
    reg_close, pm_vol_day = {}, {}
    for d in days:
        regs = [b for b in by_day[d] if _is_regular(b)]
        if regs: reg_close[d] = regs[-1]["c"]
        pmb = [b for b in by_day[d] if _is_premarket(b)]
        pm_vol_day[d] = sum(b["v"] for b in pmb)

    trades = []
    for i, d in enumerate(days):
        if d not in reg_close: continue
        prev = next((reg_close[pd] for pd in reversed(days[:i]) if pd in reg_close), None)
        if prev is None: continue
        pm = [b for b in by_day[d] if _is_premarket(b)]
        direction = _signal(pm, prev)
        if direction is None: continue

        entry = pm[-1]["c"]
        change = (entry-prev)/prev*100
        pm_vol = sum(b["v"] for b in pm)
        # RVOL: vol premarket vs promedio de hasta 10 dias previos con datos
        prior = [pm_vol_day[pd] for pd in days[:i] if pm_vol_day.get(pd, 0) > 0][-10:]
        avg = sum(prior)/len(prior) if prior else 0
        rvol = pm_vol/avg if avg > 0 else 0.0
        score, fade, _ = _code_score(pm, direction, change, pm_vol, rvol, entry,
                                     is_monday=(_weekday(pm[-1]) == 0))
        pnl = (reg_close[d]-entry)/entry*100 if direction=="LONG" else (entry-reg_close[d])/entry*100
        trades.append({"side": direction, "pnl": pnl, "ticker": ticker, "date": d,
                       "change_pm": change, "pm_vol": int(pm_vol), "rvol": round(rvol,2),
                       "early_fade": fade, "code_score": score})
    return trades


def _agg(label, trades):
    longs = [t for t in trades if t["side"]=="LONG"]
    if not longs:
        print(f"  {label:<34} sin LONG"); return
    fa = etoro_fee_analysis(longs); L = fa["long"]
    nbs = L["net_by_position_size"]
    print(f"  {label:<34} n={L['n']:>2} gross {L['gross_avg_pnl']:>+7.3f}% "
          f"WR {L['gross_wr_pct']:>3}% | NET@2k {nbs['2000']['net_avg_pnl']:>+7.3f}% "
          f"NET@5k {nbs['5000']['net_avg_pnl']:>+7.3f}%")


def fetch_earnings_blackout(ticker, start_d, end_d):
    """Set de fechas 'YYYY-MM-DD' en blackout por earnings: [E, E+1] de cada reporte."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    out = set()
    if not key:
        return out
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": start_d, "to": end_d, "symbol": ticker, "token": key},
                         timeout=15)
        r.raise_for_status()
        for e in (r.json().get("earningsCalendar") or []):
            d = e.get("date")
            if not d:
                continue
            out.add(d)
            try:
                out.add((datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
            except Exception:
                pass
    except Exception as ex:
        print(f"  [earnings] {ticker}: {ex}")
    return out


def run():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'#'*96}\n  PRE-MARKET v2 — gates de calidad | {WEEKS} sem | LONG $2 fijo "
          f"| entrada 6am Lima, salida cierre mismo dia\n{'#'*96}")

    group_of = {t: g for g, ts in GROUPS.items() for t in ts}
    all_tr = []
    for tk in TICKERS:
        bars = fetch_all(tk, "5Min", start, limit=20000)
        if not bars: continue
        tr = build_trades(tk, bars)
        for t in tr: t["group"] = group_of[tk]
        all_tr += tr

    # Tag earnings (Finnhub calendar) — blackout [E, E+1]
    start_d = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    end_d   = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for tk in TICKERS:
        bl = fetch_earnings_blackout(tk, start_d, end_d)
        for t in all_tr:
            if t["ticker"] == tk:
                t["is_earnings"] = t["date"] in bl
    for t in all_tr:
        t.setdefault("is_earnings", False)

    lc = [t for t in all_tr if t["group"]=="LARGE_CAP"]
    pm = [t for t in all_tr if t["group"]=="PUMP_SMALL"]

    # ── Detalle por senal LARGE_CAP (LONG) ──
    print(f"\n{'='*96}\n  DETALLE SENALES LARGE_CAP (LONG)\n{'='*96}")
    print(f"  {'Ticker':<6} {'Fecha':<11} {'chg%':>6} {'pm_vol':>10} {'rvol':>5} "
          f"{'fade':>5} {'code':>4} {'pnl%':>7}")
    for t in sorted([x for x in lc if x["side"]=="LONG"], key=lambda x:(x["ticker"],x["date"])):
        print(f"  {t['ticker']:<6} {t['date']:<11} {t['change_pm']:>+6.1f} {t['pm_vol']:>10,} "
              f"{t['rvol']:>5.1f} {'SI' if t['early_fade'] else '--':>5} {t['code_score']:>4} {t['pnl']:>+7.2f}")

    # ── Comparativa de gates sobre LARGE_CAP ──
    print(f"\n{'='*96}\n  IMPACTO DE GATES — LARGE_CAP LONG (mismo dia, neto fees)\n{'='*96}")
    _agg("SIN GATE (todo)", lc)
    _agg("code_score>=6", [t for t in lc if t["code_score"]>=CODE_SCORE_MIN])
    _agg("rvol>=1.5", [t for t in lc if t["rvol"]>=RVOL_FLOOR])
    _agg("excluir early_fade", [t for t in lc if not t["early_fade"]])
    _agg("code>=6 + sin fade", [t for t in lc if t["code_score"]>=CODE_SCORE_MIN and not t["early_fade"]])
    _agg("rvol>=1.5 + sin fade", [t for t in lc if t["rvol"]>=RVOL_FLOOR and not t["early_fade"]])

    # ── FILTRO EARNINGS sistematico (Finnhub) sobre todo el universo ──
    print(f"\n{'='*96}\n  FILTRO EARNINGS — calendario Finnhub, blackout [E, E+1]\n{'='*96}")
    earn_long = [t for t in all_tr if t["is_earnings"] and t["side"]=="LONG"]
    print(f"  Trades LONG marcados EARNINGS ({len(earn_long)}):")
    for t in sorted(earn_long, key=lambda x:(x["ticker"], x["date"])):
        print(f"    {t['ticker']:<6} {t['date']:<11} chg{t['change_pm']:>+6.1f}% "
              f"pnl{t['pnl']:>+7.2f}%  [{t['group']}]")
    print(f"  {'-'*88}")
    _agg("LARGE_CAP  todo", lc)
    _agg("LARGE_CAP  SIN earnings", [t for t in lc if not t["is_earnings"]])
    _agg("LARGE_CAP  solo earnings", [t for t in lc if t["is_earnings"]])
    print(f"  {'-'*88}")
    _agg("PUMP_SMALL todo", pm)
    _agg("PUMP_SMALL SIN earnings", [t for t in pm if not t["is_earnings"]])
    print(f"  {'-'*88}")
    _agg("UNIVERSO   todo", all_tr)
    _agg("UNIVERSO   SIN earnings", [t for t in all_tr if not t["is_earnings"]])
    print()


if __name__ == "__main__":
    run()
