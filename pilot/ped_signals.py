#!/usr/bin/env python3
"""
Motor de señales PED (Post-Earnings Drift) — earnings-momentum multi-día.

Segunda fuente de señal del piloto, complementaria a momentum_signals (precio).
Validado (research/backtest_ped.py + análisis out-of-sample):
  REGLA: mega-cap ≥$100B + Day+1 (reacción a earnings) ≥ +5% + sin gap-down >5%
         → entrada Day+2 OPEN, salida ~Day+7 (hold por tiempo).
  Edge ~+1.0-1.2% net/trade, WR 64-67%, robusto out-of-sample (ambas mitades 2024-26).

Semántica idéntica al piloto: la señal se detecta al CIERRE del día de reacción (ref_date)
y la compra se EJECUTA al OPEN del día siguiente (= Day+2). La salida es por número de
días hábiles en cartera (no chandelier).

Uso: integrado en run_pilot; aporta entradas/salidas al mismo PaperPortfolio.
"""
import os, sys, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Parámetros validados ───────────────────────────────────────────────────────
PED_THRESHOLD   = 5.0     # Day+1 reacción mínima (%) — refinado del playbook (+3→+5)
PED_GAP_FLOOR   = -5.0    # F5: open Day+1 no peor que -5% vs pre-close (sin resaca)
PED_HOLD_DAYS   = 6       # días hábiles en cartera: entrada Day+2 open → ~Day+8 open ≈ Day+7 close
PED_PRICED_CAP  = 25.0    # cap de sanidad: reacción >+25% = overextendido, saltar

# Universo mega-cap ≥$100B (Tipo 1 — drift institucional). Validado.
MEGA_CAP_PED = {
    "NVDA", "MSFT", "GOOG", "GOOGL", "META", "AMZN", "AAPL", "AVGO", "TSM", "ORCL",
    "NFLX", "ADBE", "CRM", "QCOM", "TXN", "AMAT", "ASML", "LRCX", "KLAC", "AMD",
    "INTC", "MU", "NOW", "PANW", "CRWD", "ARM", "PLTR", "UBER", "APP",
}

_earn_cache: dict = {}


def fetch_earnings_map(tickers, lookback_days=20):
    """{ticker: [(date 'YYYY-MM-DD', hour 'amc'|'bmo')]} de earnings recientes (yfinance)."""
    try:
        import yfinance as yf
    except Exception:
        return {}
    out = {}
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    for tk in tickers:
        if tk in _earn_cache:
            out[tk] = _earn_cache[tk]; continue
        evs = []
        try:
            df = yf.Ticker(tk).get_earnings_dates(limit=8)
            if df is not None and not df.empty:
                for idx in df.index:
                    d = idx.strftime("%Y-%m-%d")
                    if cutoff <= d <= today:
                        evs.append((d, "bmo" if idx.hour < 12 else "amc"))
        except Exception:
            pass
        _earn_cache[tk] = evs
        out[tk] = evs
        time.sleep(0.05)
    return out


def reaction_today(bars, earnings, ref_date):
    """
    Si ref_date es el día de REACCIÓN (Day+1) de algún earnings reciente, retorna
    {reaction_pct, gap, pre_close}. Si no, None.
    bars: barras diarias [{t,o,h,l,c}]; earnings: [(date, hour)].
    """
    if len(bars) < 2 or bars[-1]["t"] != ref_date:
        return None
    dates = [b["t"] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    last = len(bars) - 1
    for E, hour in earnings:
        if E not in idx:
            continue
        r = idx[E] if hour == "bmo" else idx[E] + 1   # día de reacción
        if r != last or r - 1 < 0:
            continue
        pre = bars[r - 1]["c"]
        if pre <= 0:
            continue
        return {
            "reaction_pct": (bars[r]["c"] - pre) / pre * 100,
            "gap":          (bars[r]["o"] - pre) / pre * 100,
            "pre_close":    pre,
        }
    return None


def ped_entry_candidates(data, earnings_map, ref_date):
    """
    Mega-caps cuyo Day+1 es ref_date con reacción ≥+5%, sin resaca, no overextendido.
    Estas se compran al PRÓXIMO open (= Day+2). Retorna [(ticker, reaction_pct)].
    """
    out = []
    for tk in MEGA_CAP_PED:
        bars = data.get(tk)
        if not bars:
            continue
        r = reaction_today(bars, earnings_map.get(tk, []), ref_date)
        if not r:
            continue
        if (r["reaction_pct"] >= PED_THRESHOLD
                and r["reaction_pct"] <= PED_PRICED_CAP
                and r["gap"] >= PED_GAP_FLOOR):
            out.append((tk, round(r["reaction_pct"], 1)))
    out.sort(key=lambda x: -x[1])   # mayor reacción primero
    return out


def days_held(bars, entry_date):
    """Días hábiles en cartera: cuántas barras desde entry_date hasta la última (ref_date)."""
    dates = [b["t"] for b in bars]
    if entry_date not in dates:
        return 0
    return (len(dates) - 1) - dates.index(entry_date)


def ped_should_exit(bars, entry_date, hold_days=PED_HOLD_DAYS):
    """True si la posición PED ya cumplió el hold (salida por tiempo, no chandelier)."""
    return days_held(bars, entry_date) >= hold_days
