#!/usr/bin/env python3
"""
Runner diario del piloto — momentum swing (paper trading).

Correr UNA vez al dia despues del cierre de USA (16:00 ET / 15:00 Lima).
Semantica: senales al cierre de hoy -> ordenes ejecutadas al OPEN de manana.

  python -m pilot.run_pilot              # corre, actualiza estado, alerta
  python -m pilot.run_pilot --no-alert   # sin WhatsApp (solo consola + estado)
  python -m pilot.run_pilot --rebuild-universe

Estado: data/pilot_state.json    Panel dashboard: data/pilot_dashboard.json
"""
import os, sys, json, argparse
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pilot.universe import load_universe, build_universe
from pilot.momentum_signals import (
    fetch_daily_batch, indicators, entry_candidates, chandelier_stop, macro_ok, MULT,
)
from pilot.paper_portfolio import PaperPortfolio, K, DATA_DIR
from pilot.ped_signals import (
    ped_entry_candidates, ped_should_exit, fetch_earnings_map, MEGA_CAP_PED,
)
from pilot.star_score import compute_top, sector_strength, SECTOR_ETFS

DASH_PATH = os.path.join(DATA_DIR, "pilot_dashboard.json")


def _last_bar_maps(data, ref_date):
    """open/high/close/atr% por ticker SOLO si su ultima barra es de ref_date."""
    opn, hi, cl = {}, {}, {}
    for tk, bars in data.items():
        if bars and bars[-1]["t"] == ref_date:
            opn[tk], hi[tk], cl[tk] = bars[-1]["o"], bars[-1]["h"], bars[-1]["c"]
    return opn, hi, cl


def run(send_alert=True):
    uni = load_universe()
    fetch_list = sorted(set(uni) | MEGA_CAP_PED)   # incluir mega-caps PED en el fetch
    data = fetch_daily_batch(fetch_list + ["QQQ"] + SECTOR_ETFS)
    qqq = data.pop("QQQ", [])
    sector_bars = {e: data.pop(e, []) for e in SECTOR_ETFS}   # ETFs de sector fuera del trading
    if not qqq:
        print("[pilot] sin datos de QQQ — abortando"); return
    ref_date = qqq[-1]["t"]

    inds = {tk: indicators(b) for tk, b in data.items()}
    inds = {k: v for k, v in inds.items() if v}
    opn, hi, cl = _last_bar_maps(data, ref_date)

    # momentum por sector (ETF) + flag ⭐ TOP (tercil superior del score validado)
    sector_mom = {e: (indicators(b) or {}).get("momentum") for e, b in sector_bars.items()}
    sector_mom = {e: m for e, m in sector_mom.items() if m is not None}

    pp = PaperPortfolio.load()
    pp.s.setdefault("live_start_date", ref_date)   # frontera seed-backfill / live-forward
    star_rows = []
    for tk, ind in inds.items():
        if tk in pp.positions or (ind.get("above_sma") and ind.get("is_breakout")):
            if ind.get("momentum") is not None and ind.get("sma200"):
                star_rows.append({"tk": tk, "mom": ind["momentum"], "trend": ind["price"]/ind["sma200"]-1})
    top_set, _ = compute_top(star_rows, sector_mom)

    if pp.s["last_run_date"] == ref_date:
        print(f"[pilot] ya corrido para {ref_date} — nada que hacer")
        return _emit_dashboard(pp, inds, cl, qqq, ref_date, [], top_set=top_set,
                               sector_mom=sector_mom, send_alert=False)

    actions = []   # fills de hoy (ordenes que estaban pendientes)
    earnings_map = fetch_earnings_map([t for t in MEGA_CAP_PED if t in data])

    # ── 1) EJECUTAR ordenes pendientes (generadas ayer) al OPEN de hoy ──────────
    pend = pp.pending
    for tk in pend.get("sells", []):
        if tk in pp.positions and tk in opn:
            a = pp.close_position(tk, opn[tk], ref_date)
            if a: actions.append(a)
    equity_now = pp.equity(cl)
    for tk in pend.get("buys", []):                       # entradas Marea (momentum)
        if pp.free_slots <= 0: break
        if tk in opn and tk not in pp.positions:
            ind = inds.get(tk); atr_pct = (ind["atr"]/ind["price"]) if ind and ind["atr"] else None
            a = pp.open_position(tk, opn[tk], equity_now, atr_pct, ref_date)
            if a: actions.append(a)
    for tk in pend.get("ped_buys", []):                   # entradas PED (earnings)
        if pp.free_slots <= 0: break
        if tk in opn and tk not in pp.positions:
            ind = inds.get(tk); atr_pct = (ind["atr"]/ind["price"]) if ind and ind["atr"] else None
            a = pp.open_position(tk, opn[tk], equity_now, atr_pct, ref_date, source="ped")
            if a: actions.append(a)

    # ── 2) SALIDAS: chandelier (Marea) o por tiempo ~Day+7 (PED) ────────────────
    new_sells = []
    for tk in list(pp.positions):
        if tk in hi: pp.update_high(tk, hi[tk])
        pos = pp.positions[tk]
        if pos.get("source") == "ped":
            if ped_should_exit(data.get(tk, []), pos["entry_date"]):
                new_sells.append(tk)
            continue
        ind = inds.get(tk)
        if not ind or ind["atr"] is None or tk not in cl: continue
        stop = chandelier_stop(pos["hh"], ind["atr"])
        if cl[tk] < stop:
            new_sells.append(tk)

    # ── 3) ENTRADAS nuevas: Marea (momentum, prioridad) + PED (earnings) ────────
    macro = macro_ok(qqq)
    held = set(pp.positions)
    slots_next = K - (len(held) - len(new_sells))
    new_buys, new_ped_buys = [], []
    if slots_next > 0:
        if macro:   # Marea solo en régimen alcista (QQQ > SMA200)
            cands = [tk for tk in entry_candidates(inds) if tk not in held and tk in cl]
            new_buys = cands[:slots_next]
        # PED usa los slots que Marea no tomó (earnings es idiosincrático, no depende del macro)
        slots_ped = slots_next - len(new_buys)
        if slots_ped > 0:
            ped_cands = [tk for tk, _ in ped_entry_candidates(data, earnings_map, ref_date)
                         if tk not in held and tk not in new_buys and tk in cl]
            new_ped_buys = ped_cands[:slots_ped]

    pp.s["pending"] = {"buys": new_buys, "sells": new_sells, "ped_buys": new_ped_buys}

    # ── 4) marcar equity al cierre + persistir ──────────────────────────────────
    eq = pp.record_equity(ref_date, cl)
    pp.s["last_run_date"] = ref_date
    pp.save()

    _print_console(pp, actions, new_buys, new_sells, new_ped_buys, cl, eq, macro, ref_date)
    return _emit_dashboard(pp, inds, cl, qqq, ref_date, actions,
                           new_buys=new_buys, new_sells=new_sells, new_ped_buys=new_ped_buys,
                           top_set=top_set, sector_mom=sector_mom, macro=macro, send_alert=send_alert)


# ── salida: consola, dashboard json, alerta whatsapp ───────────────────────────

def _pos_lines(pp, cl):
    out = []
    for tk, p in pp.positions.items():
        px = cl.get(tk, p["entry_price"])
        pnl = (px/p["entry_price"]-1)*100
        out.append((tk, round(pnl, 1), p["entry_price"], round(px, 2)))
    return sorted(out, key=lambda x: -x[1])


def _print_console(pp, actions, buys, sells, ped_buys, cl, eq, macro, date):
    init = pp.s["capital_initial"]
    print(f"\n=== MOMENTUM MULTI-DÍA (Marea + PED) — {date} ===")
    print(f"Equity ${eq:,.0f}  ({(eq/init-1)*100:+.1f}% desde inicio)  | cash ${pp.cash:,.0f} | "
          f"posiciones {len(pp.positions)}/{K} | macro {'ALCISTA' if macro else 'BAJISTA (sin nuevas Marea)'}")
    if actions:
        print("Ejecutado hoy (al open):")
        for a in actions:
            if a["action"]=="BUY": print(f"  COMPRA {a['ticker']} [{a.get('source','marea')}] @ ${a['price']} (${a['cost']:.0f})")
            else: print(f"  VENTA  {a['ticker']} @ ${a['price']} ({a['pnl_pct']:+.1f}%, ${a['pnl_usd']:+.0f})")
    print(f"MANANA AL ABRIR -> Marea: {buys or '—'} | PED: {ped_buys or '—'} | vender: {sells or '—'}")
    pl = _pos_lines(pp, cl)
    if pl:
        print("En cartera:")
        for tk,pnl,ent,px in pl:
            print(f"  {tk:6} {pnl:+5.1f}%  (entrada ${ent} -> ${px})")


def _build_alert(pp, cl, eq, actions, buys, sells, ped_buys, macro, date):
    init = pp.s["capital_initial"]
    L = [f"📈 *MOMENTUM MULTI-DÍA* — {date}",
         f"Equity ${eq:,.0f} ({(eq/init-1)*100:+.1f}%) | {len(pp.positions)}/{K} pos"]
    done = [a for a in actions]
    if done:
        L.append("")
        for a in done:
            if a["action"]=="BUY": L.append(f"✅ COMPRADO {a['ticker']} [{a.get('source','marea')}] @ ${a['price']}")
            else: L.append(f"💰 VENDIDO {a['ticker']} ({a['pnl_pct']:+.1f}%)")
    if buys or sells or ped_buys:
        L.append("\n➡️ *MAÑANA AL ABRIR:*")
        if buys:     L.append(f"🟢 COMPRAR (Marea): {', '.join(buys)}")
        if ped_buys: L.append(f"🟣 PED (earnings): {', '.join(ped_buys)}")
        if sells:    L.append(f"🔴 VENDER: {', '.join(sells)}")
    if not macro:
        L.append("\n⚠️ Mercado bajo SMA200 — sin nuevas entradas")
    pl = _pos_lines(pp, cl)
    if pl:
        L.append("\n📊 En cartera:")
        for tk,pnl,ent,px in pl[:8]:
            L.append(f"• {tk} {pnl:+.1f}%")
    return "\n".join(L)


def _emit_dashboard(pp, inds, cl, qqq, date, actions, new_buys=None, new_sells=None,
                    new_ped_buys=None, top_set=None, sector_mom=None, macro=True, send_alert=True):
    top_set = top_set or set()
    eh = pp.s["equity_history"]
    eq = eh[-1]["equity"] if eh else pp.cash
    # benchmark QQQ sobre el mismo span del piloto (honestidad: ¿alpha o beta?)
    qqq_ret = None
    if eh and qqq:
        qmap = {b["t"]: b["c"] for b in qqq}
        q0 = qmap.get(eh[0]["date"]); q1 = qmap.get(date) or qqq[-1]["c"]
        if q0 and q1: qqq_ret = round((q1/q0 - 1)*100, 1)
    realized   = round(sum(t.get("pnl_usd", 0) for t in pp.s["closed_trades"]), 0)
    unrealized = round(sum(p["shares"]*(cl.get(tk, p["entry_price"]) - p["entry_price"])
                           for tk, p in pp.positions.items()), 0)
    live_start = pp.s.get("live_start_date")
    live_ret = None
    if live_start and eh:
        base = next((e["equity"] for e in eh if e["date"] >= live_start), None)
        if base: live_ret = round((eq/base - 1)*100, 1)
    dash = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "as_of": date,
        "capital_initial": pp.s["capital_initial"],
        "equity": eq,
        "total_return_pct": round((eq/pp.s["capital_initial"]-1)*100, 2),
        "qqq_return_pct": qqq_ret,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "live_start_date": live_start,
        "live_return_pct": live_ret,
        "cash": round(pp.cash, 2),
        "macro_bullish": macro,
        "positions": [
            {"ticker": tk, "pnl_pct": pnl, "entry": ent, "price": px,
             "source": pp.positions.get(tk, {}).get("source", "marea"),
             "top": tk in top_set}
            for tk, pnl, ent, px in _pos_lines(pp, cl)
        ],
        "pending": pp.pending,
        "star_top": sorted(top_set),                       # tickers ⭐ TOP (alta convicción, validado)
        "sector_strength": sector_strength(sector_mom or {}),
        "actions_today": actions,
        "closed_trades": pp.s["closed_trades"][-20:],
        "equity_history": pp.s["equity_history"][-260:],
    }
    with open(DASH_PATH, "w") as f:
        json.dump(dash, f, indent=2)
    print(f"[pilot] dashboard -> {DASH_PATH}")

    if send_alert:
        # El número destino vive en config.json (api_keys.twilio_to), igual que en main.py.
        # Respaldo a env TWILIO_TO. Sin esto, el cron generaba recomendaciones pero no avisaba.
        to = os.environ.get("TWILIO_TO", "")
        if not to:
            try:
                _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
                to = json.load(open(_cfg_path, encoding="utf-8")).get("api_keys", {}).get("twilio_to", "")
            except Exception:
                to = ""
        body = _build_alert(pp, cl, eq, actions, new_buys or pp.pending.get("buys"),
                            new_sells or pp.pending.get("sells"),
                            new_ped_buys if new_ped_buys is not None else pp.pending.get("ped_buys", []),
                            macro, date)
        if to:
            try:
                from alerts.twilio_sms import send_raw_message
                ok = send_raw_message(body, to)
                print(f"[pilot] alerta WhatsApp: {'enviada' if ok else 'fallo'}")
            except Exception as e:
                print(f"[pilot] alerta error: {e}")
        else:
            print("[pilot] TWILIO_TO no configurado — alerta no enviada (se configura en la VM)")
            print("---- preview alerta ----"); print(body); print("------------------------")
    return dash


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--rebuild-universe", action="store_true")
    args = ap.parse_args()
    if args.rebuild_universe:
        build_universe()
    run(send_alert=not args.no_alert)
