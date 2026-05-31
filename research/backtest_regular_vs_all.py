import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
Comparativa: Horario Regular (IEX real-time) vs Todo el dia (SIP con delay).
Solo timeframe 1m (el mejor segun super_backtest.py).
10 tickers x 1 semana de historia.

Horario regular: 9:30am-4:00pm ET = 8:30am-3:00pm Lima = 13:30-20:00 UTC (EDT).
"""
import os, sys, time as _time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from utils.signal_engine import compute_signal, compute_htf_trend, DIRECTION_LOCK_SCANS

ALPACA_BASE      = "https://data.alpaca.markets"
LIMA_OFFSET      = -5
CONFIRM_REQ      = 2
MAX_SCANS        = 30   # time-stop 1m: 30 scans = 30 min
ADVERSE_PCT      = 0.5
WARMUP           = 30
TODAY_DATE       = "2026-05-27"

TICKERS = ["NVDA", "AMD", "MU", "PLTR", "IONQ", "QBTS", "APP", "RDDT", "AVGO", "MSFT"]


# -- Helpers --------------------------------------------------------------------

def _hdrs():
    return {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


def fetch_all(ticker, tf, start, limit=8000):
    bars, params = [], {
        "timeframe": tf, "start": start, "feed": "sip",
        "limit": 1000, "sort": "asc",
    }
    try:
        while len(bars) < limit:
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=_hdrs(), timeout=30)
            r.raise_for_status()
            data  = r.json()
            chunk = data.get("bars") or []
            bars.extend(chunk)
            if not data.get("next_page_token"):
                break
            params["page_token"] = data["next_page_token"]
            _time.sleep(0.15)
    except Exception:
        pass
    return [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": int(b["v"])} for b in bars]


def lima_ts(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%H:%M")


def lima_date(bar):
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    return (dt + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d")


def is_regular(bar):
    """True si la barra cae dentro de horario regular NYSE (9:30am-4pm ET)."""
    dt = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    if dt.weekday() >= 5:
        return False
    is_edt = 3 <= dt.month <= 11
    et = dt - timedelta(hours=4 if is_edt else 5)
    dm = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= dm < 16 * 60


def is_last_regular_of_day(bars, i):
    """True si bars[i] es la ultima barra regular del dia."""
    if not is_regular(bars[i]):
        return False
    if i + 1 >= len(bars):
        return True
    return not is_regular(bars[i + 1]) or lima_date(bars[i + 1]) != lima_date(bars[i])


# -- Simulacion -----------------------------------------------------------------

def simulate(bars, htf_bars, regular_only=False):
    """
    Simula barra a barra con todas las mejoras v3.
    regular_only=True: bloquea entradas fuera de horario regular y
                       fuerza cierre EOD al final de la sesion regular.
    """
    cand_sig   = None
    cand_count = 0
    cur_sig    = "ESPERAR"
    lock_rem   = 0
    lock_dir   = None
    position   = None
    trades     = []

    for i in range(WARMUP, len(bars)):
        candles   = bars[:i + 1]
        bar       = candles[-1]
        cur_ts    = bar["t"]
        cur_price = bar["c"]
        bar_reg   = is_regular(bar)

        htf_avail = [b for b in htf_bars if b["t"] < cur_ts]
        htf_trend = compute_htf_trend(htf_avail) if len(htf_avail) >= 20 else "NEUTRAL"

        # EOD force-close: ultima barra regular del dia con posicion abierta
        if regular_only and position and is_last_regular_of_day(bars, i):
            ep_pos = position["entry_price"]
            side   = position["side"]
            pnl    = ((cur_price - ep_pos) / ep_pos * 100 if side == "LONG"
                      else (ep_pos - cur_price) / ep_pos * 100)
            trades.append({
                "side": side, "in_t": position["entry_time"], "in_p": ep_pos,
                "out_t": lima_ts(bar) + "c", "out_p": cur_price,
                "pnl": round(pnl, 3), "note": "EOD-CLOSE", "date": position.get("date", lima_date(bar)),
            })
            position = None; lock_dir = None; lock_rem = 0
            cur_sig  = "ESPERAR"; cand_sig = None; cand_count = 0
            continue

        # Time-stop
        if position:
            position["scans_held"] = position.get("scans_held", 0) + 1
            ep_pos   = position["entry_price"]
            adverse  = ((ep_pos - cur_price) / ep_pos * 100 if position["side"] == "LONG"
                        else (cur_price - ep_pos) / ep_pos * 100)
            if position["scans_held"] >= MAX_SCANS and adverse >= ADVERSE_PCT:
                no   = bars[i + 1]["o"] if i + 1 < len(bars) else cur_price
                nt   = lima_ts(bars[i + 1]) if i + 1 < len(bars) else lima_ts(bar)
                side = position["side"]
                pnl  = ((no - ep_pos) / ep_pos * 100 if side == "LONG"
                        else (ep_pos - no) / ep_pos * 100)
                trades.append({
                    "side": side, "in_t": position["entry_time"], "in_p": ep_pos,
                    "out_t": nt, "out_p": no, "pnl": round(pnl, 3),
                    "note": "TIME-STOP", "date": lima_date(bar),
                })
                position = None; lock_dir = None; lock_rem = 0
                cur_sig  = "ESPERAR"; cand_sig = None; cand_count = 0
                continue

        pos_state = "SIN_POSICION" if not position else position["side"]
        res       = compute_signal(candles, pos_state, None, htf_trend)
        raw_sig   = res["signal"]

        # Direction lock
        if lock_rem > 0 and lock_dir:
            if (lock_dir == "SHORT" and raw_sig == "ENTRAR_SHORT") or \
               (lock_dir == "LONG"  and raw_sig == "ENTRAR_LONG"):
                raw_sig = "ESPERAR"
        if lock_rem > 0:
            lock_rem -= 1
            if lock_rem == 0:
                lock_dir = None

        # Bloquear ENTRAR fuera de horario regular (solo en modo regular_only)
        if regular_only and not bar_reg and raw_sig in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            raw_sig = "ESPERAR"

        # Confirmacion 2 scans
        confirmed = False
        if raw_sig == cand_sig:
            cand_count += 1
            if cand_count >= CONFIRM_REQ and raw_sig != cur_sig:
                confirmed = True; cur_sig = raw_sig
            if cand_count >= CONFIRM_REQ:
                cand_sig = None; cand_count = 0
        else:
            cand_sig = raw_sig; cand_count = 1

        if not confirmed:
            continue

        if cur_sig == "ENTRAR_LONG":
            lock_dir = "SHORT"; lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig == "ENTRAR_SHORT":
            lock_dir = "LONG";  lock_rem = DIRECTION_LOCK_SCANS
        elif cur_sig in ("CERRAR_LONG", "CERRAR_SHORT", "ESPERAR"):
            lock_dir = None; lock_rem = 0

        ep = bars[i + 1]["o"] if i + 1 < len(bars) else bar["c"]
        et = lima_ts(bars[i + 1]) if i + 1 < len(bars) else lima_ts(bar)
        ed = lima_date(bars[i + 1]) if i + 1 < len(bars) else lima_date(bar)

        def close_pos(ep, et, ed, note=""):
            if not position: return
            side = position["side"]
            pnl  = (ep - position["entry_price"]) / position["entry_price"] * 100
            if side == "SHORT": pnl = -pnl
            trades.append({
                "side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
                "out_t": et, "out_p": ep, "pnl": round(pnl, 3), "note": note,
                "date": position.get("date", ed),
            })

        if cur_sig == "ENTRAR_LONG":
            if position and position["side"] == "SHORT":
                close_pos(ep, et, ed, "inversion"); position = None
            if not position:
                position = {"side": "LONG", "entry_price": ep, "entry_time": et,
                            "scans_held": 0, "date": ed}
        elif cur_sig == "ENTRAR_SHORT":
            if position and position["side"] == "LONG":
                close_pos(ep, et, ed, "inversion"); position = None
            if not position:
                position = {"side": "SHORT", "entry_price": ep, "entry_time": et,
                            "scans_held": 0, "date": ed}
        elif cur_sig == "CERRAR_LONG" and position and position["side"] == "LONG":
            close_pos(ep, et, ed); position = None
        elif cur_sig == "CERRAR_SHORT" and position and position["side"] == "SHORT":
            close_pos(ep, et, ed); position = None

    # Cierre al fin del periodo
    if position:
        lp   = bars[-1]["c"]
        side = position["side"]
        pnl  = (lp - position["entry_price"]) / position["entry_price"] * 100
        if side == "SHORT": pnl = -pnl
        trades.append({
            "side": side, "in_t": position["entry_time"], "in_p": position["entry_price"],
            "out_t": lima_ts(bars[-1]) + "*", "out_p": lp,
            "pnl": round(pnl, 3), "note": "fin_periodo", "date": position.get("date", "?"),
        })

    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "wins": 0, "pct_win": 0, "total": 0,
                "avg": 0, "best": 0, "worst": 0, "tstops": 0,
                "eod": 0, "avg_win": 0, "avg_loss": 0}
    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total  = sum(pnls)
    return {
        "n":        len(trades),
        "wins":     len(wins),
        "pct_win":  round(len(wins) / len(trades) * 100),
        "total":    round(total, 2),
        "avg":      round(total / len(trades), 3),
        "best":     round(max(wins,   default=0), 2),
        "worst":    round(min(losses, default=0), 2),
        "tstops":   sum(1 for t in trades if t.get("note") == "TIME-STOP"),
        "eod":      sum(1 for t in trades if t.get("note") == "EOD-CLOSE"),
        "avg_win":  round(sum(wins)   / len(wins)   if wins   else 0, 3),
        "avg_loss": round(sum(losses) / len(losses) if losses else 0, 3),
    }


def fmt_pnl(v, w=8):
    s = f"{'+' if v >= 0 else ''}{v:.2f}%"
    return s.rjust(w)


def run():
    W = 86
    now_str = (datetime.now(timezone.utc) + timedelta(hours=LIMA_OFFSET)).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*W}")
    print(f"  BACKTEST: TODO EL DIA vs SOLO HORARIO REGULAR")
    print(f"  {now_str} Lima  |  TF: 1m  |  {len(TICKERS)} tickers  |  1 semana")
    print(f"  Time-stop: {MAX_SCANS} scans / {ADVERSE_PCT}% adverso")
    print(f"{'='*W}\n")

    start_1m  = (datetime.now(timezone.utc) - timedelta(weeks=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start_htf = (datetime.now(timezone.utc) - timedelta(weeks=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_trades_full    = []
    all_trades_regular = []
    ticker_rows        = []

    for ticker in TICKERS:
        sys.stdout.write(f"  {ticker}... ")
        sys.stdout.flush()

        bars1m = fetch_all(ticker, "1Min", start_1m)
        bars5m = fetch_all(ticker, "5Min", start_htf)

        if len(bars1m) < WARMUP + 5:
            print(f"sin datos ({len(bars1m)} barras)")
            continue

        t_full    = simulate(bars1m, bars5m, regular_only=False)
        t_regular = simulate(bars1m, bars5m, regular_only=True)

        s_full    = stats(t_full)
        s_regular = stats(t_regular)

        all_trades_full    += t_full
        all_trades_regular += t_regular

        # Contar barras regulares
        reg_bars = sum(1 for b in bars1m if is_regular(b))

        ticker_rows.append({
            "ticker": ticker,
            "bars": len(bars1m),
            "reg_bars": reg_bars,
            "full": s_full,
            "reg": s_regular,
        })

        delta_pnl = s_regular["total"] - s_full["total"]
        delta_wr  = s_regular["pct_win"] - s_full["pct_win"]
        print(f"{len(bars1m)} barras ({reg_bars} reg) | "
              f"Full: {s_full['n']}t {s_full['pct_win']}% WR {fmt_pnl(s_full['total']).strip()} | "
              f"Reg: {s_regular['n']}t {s_regular['pct_win']}% WR {fmt_pnl(s_regular['total']).strip()} | "
              f"delta {'+' if delta_pnl>=0 else ''}{delta_pnl:.2f}pp WR {'+' if delta_wr>=0 else ''}{delta_wr}pp")

    sf = stats(all_trades_full)
    sr = stats(all_trades_regular)

    # ── Tabla detallada por ticker ─────────────────────────────────────────────
    print(f"\n{'-'*W}")
    print(f"  {'Ticker':<6}  {'Barras':>6} {'RegBars':>7} |"
          f"  {'N':>3} {'WR':>4} {'PnL':>8} {'Avg':>7} {'Best':>6} {'Worst':>7} |"
          f"  {'N':>3} {'WR':>4} {'PnL':>8} {'Avg':>7} {'Best':>6} {'Worst':>7}  {'Delta':>7}")
    print(f"  {'':6}  {'':6} {'':7} |"
          f"  {'<-':>3} {'TODO':>4} {'EL DIA':>8} {'':>7} {'':>6} {'':>7} |"
          f"  {'<-':>3} {'SOLO':>4} {'REGULAR':>8} {'':>7} {'':>6} {'':>7}  {'PnL':>7}")
    print(f"  {'-'*82}")

    for r in ticker_rows:
        sf_t = r["full"]
        sr_t = r["reg"]
        delta = sr_t["total"] - sf_t["total"]
        d_s   = f"{'+' if delta>=0 else ''}{delta:.2f}%"
        print(f"  {r['ticker']:<6}  {r['bars']:>6} {r['reg_bars']:>7} |"
              f"  {sf_t['n']:>3} {sf_t['pct_win']:>3}% {fmt_pnl(sf_t['total'])} {sf_t['avg']:>+6.3f}%"
              f" {sf_t['best']:>+5.2f}% {sf_t['worst']:>+6.2f}% |"
              f"  {sr_t['n']:>3} {sr_t['pct_win']:>3}% {fmt_pnl(sr_t['total'])} {sr_t['avg']:>+6.3f}%"
              f" {sr_t['best']:>+5.2f}% {sr_t['worst']:>+6.2f}%  {d_s:>7}")

    print(f"  {'-'*82}")
    delta_total = sr["total"] - sf["total"]
    delta_wr    = sr["pct_win"] - sf["pct_win"]
    d_tot_s     = f"{'+' if delta_total>=0 else ''}{delta_total:.2f}%"
    print(f"  {'TOTAL':<6}  {'-':>6} {'-':>7} |"
          f"  {sf['n']:>3} {sf['pct_win']:>3}% {fmt_pnl(sf['total'])} {sf['avg']:>+6.3f}%"
          f" {sf['best']:>+5.2f}% {sf['worst']:>+6.2f}% |"
          f"  {sr['n']:>3} {sr['pct_win']:>3}% {fmt_pnl(sr['total'])} {sr['avg']:>+6.3f}%"
          f" {sr['best']:>+5.2f}% {sr['worst']:>+6.2f}%  {d_tot_s:>7}")

    # ── Comparativa resumen ────────────────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  COMPARATIVA FINAL - 1m  |  10 tickers  |  1 semana")
    print(f"{'='*W}")
    cols = ["Trades", "Win Rate", "P&L Total", "Avg/trade", "Mejor trade", "Peor trade", "Time-stops", "EOD closes"]
    vals_full = [
        str(sf["n"]),
        f"{sf['pct_win']}%",
        fmt_pnl(sf["total"]).strip(),
        f"{sf['avg']:+.3f}%",
        f"{sf['best']:+.2f}%",
        f"{sf['worst']:+.2f}%",
        str(sf["tstops"]),
        str(sf["eod"]),
    ]
    vals_reg = [
        str(sr["n"]),
        f"{sr['pct_win']}%",
        fmt_pnl(sr["total"]).strip(),
        f"{sr['avg']:+.3f}%",
        f"{sr['best']:+.2f}%",
        f"{sr['worst']:+.2f}%",
        str(sr["tstops"]),
        str(sr["eod"]),
    ]

    print(f"\n  {'Metrica':<16}  {'TODO EL DIA':>14}  {'SOLO REGULAR':>14}  {'Diferencia':>12}")
    print(f"  {'-'*60}")
    metrics_compare = list(zip(cols, vals_full, vals_reg))
    diffs = [
        f"{delta_wr:+}pp",
        f"{delta_total:+.2f}pp",
        f"{sr['avg']-sf['avg']:+.3f}pp",
        f"{sr['best']-sf['best']:+.2f}pp",
        f"{sr['worst']-sf['worst']:+.2f}pp",
        "", "",
    ]
    for i, (col, vf, vr) in enumerate(metrics_compare):
        diff = diffs[i - 1] if i > 0 else ""
        marker = " <--" if i > 0 and ("pp" in diff and not diff.startswith("-0.00") and not diff.startswith("+0.00") and not diff.startswith("-0.000")) else ""
        print(f"  {col:<16}  {vf:>14}  {vr:>14}  {diff:>12}{marker}")

    # ── Distribucion horaria de los trades ─────────────────────────────────────
    print(f"\n  DISTRIBUCION DE TRADES POR HORA (todo el dia vs solo regular):")
    print(f"  Hora Lima  {'Trades':>6}  {'WR':>5}  {'PnL':>8}  {'Zona':>16}")
    print(f"  {'-'*48}")
    by_hour = {}
    for t in all_trades_full:
        h = t["in_t"][:2] if t["in_t"] else "??"
        if h not in by_hour: by_hour[h] = []
        by_hour[h].append(t["pnl"])
    for h in sorted(by_hour.keys()):
        pnls = by_hour[h]
        wins = sum(1 for p in pnls if p > 0)
        total_h = sum(pnls)
        wr_h = round(wins / len(pnls) * 100) if pnls else 0
        hi = int(h)
        # Clasificar sesion (Lima = UTC-5, ET = Lima+0 en EDT)
        # Regular: 8:30-15:00 Lima (EDT), Pre-market: 3:00-8:30, AH: 15:00-20:00
        if 8 <= hi < 15:
            zona = "REGULAR (IEX)"
            col  = "***"
        elif 3 <= hi < 8:
            zona = "Pre-market (SIP)"
            col  = "   "
        else:
            zona = "After-hours (SIP)"
            col  = "   "
        pnl_str = f"{'+' if total_h>=0 else ''}{total_h:.2f}%"
        print(f"  {h}:00-{h}:59    {len(pnls):>6}  {wr_h:>4}%  {pnl_str:>8}  {col} {zona}")

    print()
    reg_trades_count = sum(1 for t in all_trades_full
                           if t["in_t"][:2].isdigit() and 8 <= int(t["in_t"][:2]) < 15)
    nonreg_trades_count = sf["n"] - reg_trades_count
    print(f"  Trades en horario regular: {reg_trades_count} ({round(reg_trades_count/sf['n']*100) if sf['n'] else 0}% del total)")
    print(f"  Trades fuera de regular:   {nonreg_trades_count} ({round(nonreg_trades_count/sf['n']*100) if sf['n'] else 0}% del total)")
    print()


if __name__ == "__main__":
    run()
