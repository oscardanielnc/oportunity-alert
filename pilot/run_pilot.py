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
    entry_levels,
)
from pilot.paper_portfolio import PaperPortfolio, K, VOL_TARGET, DATA_DIR
from pilot.ped_signals import (
    ped_entry_candidates, ped_should_exit, fetch_earnings_map, MEGA_CAP_PED,
    days_held, PED_HOLD_DAYS,
)
from pilot.star_score import compute_top, sector_strength, SECTOR_ETFS, SECTOR_ETF, SECTOR_NAME, DEFAULT_ETF

DASH_PATH = os.path.join(DATA_DIR, "pilot_dashboard.json")
NEWS_FLAG_LOG = os.path.join(DATA_DIR, "pilot_news_flags.jsonl")  # log informativo (punto 4)


def _news_sector_ctx(tks, sector_mom, metrics=None):
    """
    Contexto INFORMATIVO por ticker: estado del sector (momentum + ranking + a favor/en
    contra) + noticia adversa reciente (5 días, desde la metrics.db del brazo de noticias).
    NO veta — solo informa. Best-effort: si algo falla, ese ticker va sin contexto.
    """
    ranked = sorted((e for e, m in sector_mom.items() if m is not None),
                    key=lambda e: -sector_mom[e])
    rank_of = {e: i for i, e in enumerate(ranked)}

    if metrics is None:
        try:
            from utils.metrics_store import MetricsStore
            metrics = MetricsStore(os.path.join(DATA_DIR, "metrics.db"))
        except Exception:
            metrics = None

    out = {}
    for tk in tks:
        ctx = {}
        etf = SECTOR_ETF.get(tk, DEFAULT_ETF)
        mom = sector_mom.get(etf)
        if mom is not None:
            pct = mom * 100
            ctx["sector_name"] = SECTOR_NAME.get(etf, etf)
            ctx["sector_mom_pct"] = round(pct, 1)
            ctx["sector_favor"] = pct >= 0
            if ranked:
                ctx["sector_rank"] = rank_of.get(etf, len(ranked)) + 1
                ctx["sector_total"] = len(ranked)
        if metrics is not None:
            try:
                recent = metrics.get_recent_news_for_ticker(tk, minutes=5 * 24 * 60)
                adverse = next((n for n in recent if (n.get("direccion") or "").upper() == "SHORT"), None)
                if adverse:
                    ctx["adverse_news"] = (adverse.get("resumen_cataliz") or "")[:120]
                    ctx["adverse_ts"] = adverse.get("ts")
            except Exception:
                pass
        if ctx:
            out[tk] = ctx
    return out


def _buy_context(pend_buys, sector_mom):
    """Contexto de las COMPRAS recomendadas + log informativo (punto 4, Oscar 2026-06-01)."""
    out = _news_sector_ctx(pend_buys, sector_mom)
    try:
        flagged = {tk: c for tk, c in out.items() if c.get("adverse_news")}
        if flagged:
            with open(NEWS_FLAG_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "flagged": flagged,
                }, ensure_ascii=False) + "\n")
            print(f"[pilot] {len(flagged)} compra(s) con noticia adversa reciente: {list(flagged)}")
    except Exception:
        pass
    return out


def _size_pct(ind):
    """
    % del capital sugerido para una COMPRA (vol-sizing). MISMO cálculo que
    paper_portfolio.open_position: base = equity/K (20%), escalado por min(1, VOL_TARGET/atr%)
    → los nombres más volátiles entran más chicos (riesgo parejo por posición), tope 20% (sin
    apalancar). Se expone para que Oscar replique el tamaño en eToro (antes solo se calculaba
    internamente y no se mostraba). % agnóstico al equity: multiplicarlo por el capital real.
    """
    if not ind or not ind.get("atr") or not ind.get("price"):
        return None
    atr_pct = ind["atr"] / ind["price"]
    if atr_pct <= 0:
        return None
    return round(min(1.0, VOL_TARGET / atr_pct) * 100.0 / K, 1)


def _leaders(pp, inds, cl, top_set, sector_mom, held_days, pending, n=10):
    """
    Top-N líderes por fuerza relativa (momentum 126d, en tendencia QQB>SMA200), con la
    info para que Oscar decida DISCRECIONALMENTE qué rotar (decisión Oscar 2026-06-01:
    rotación automática rechazada por backtest — más DD/menos Sharpe; este panel es la vía).

    Incluye además los holdings que CAYERON fuera del top-N (candidatos a rotar). Cada fila
    trae acción del día (comprar/vender al open), niveles desplegables, sector y noticia.
    """
    top_set = top_set or set()
    ranked = sorted(
        [(ind["momentum"], tk) for tk, ind in inds.items()
         if ind.get("momentum") is not None and ind.get("above_sma")],
        reverse=True)
    leader_tks = [tk for _, tk in ranked[:n]]
    # Garantizar visibilidad de holdings y compras pendientes aunque caigan fuera del top-N
    must = list(pp.positions) + (pending.get("buys") or []) + (pending.get("ped_buys") or [])
    extra = [tk for tk in dict.fromkeys(must) if tk not in leader_tks]
    all_tks = leader_tks + extra

    ctx = _news_sector_ctx(all_tks, sector_mom or {})
    buys = set((pending.get("buys") or []) + (pending.get("ped_buys") or []))
    sells = set(pending.get("sells") or [])

    out = []
    for i, tk in enumerate(all_tks):
        ind = inds.get(tk) or {}
        in_top = tk in leader_tks
        held_pos = pp.positions.get(tk)
        row = {
            "rank": (i + 1) if in_top else None,
            "ticker": tk,
            "momentum_pct": round(ind["momentum"] * 100, 1) if ind.get("momentum") is not None else None,
            "in_top": in_top,
            "held": bool(held_pos),
            "source": (held_pos or {}).get("source"),
            "is_top": tk in top_set,
            "is_breakout": bool(ind.get("is_breakout")),
            "action": "buy" if tk in buys else ("sell" if tk in sells else None),
            "size_pct": _size_pct(ind),                 # % del capital sugerido (vol-sizing)
            "entry": entry_levels(ind) or {},
        }
        row.update(ctx.get(tk) or {})
        if held_pos:
            px = cl.get(tk, held_pos["entry_price"])
            row["pnl_pct"] = round((px / held_pos["entry_price"] - 1) * 100, 1)
            row["days_held"] = held_days.get(tk)
            row["stop_4atr"] = _live_chandelier(pp, inds, tk)
        out.append(row)
    return out


def _fresh_breakouts(pp, inds, cl, top_set, sector_mom, leaders, n=8):
    """
    Breakouts NUEVOS invisibles en el top-10: sobre SMA200 + nuevo máximo 50d pero con
    momentum 6m bajo (recién despiertan) → no rankean como líderes. Caso SNOW (2026-06-01):
    explotó +58% en días pero su momentum 126d es solo +11.6%, así que quedaba oculto.
    Informativo (candidatos de entrada si se libera un slot) — NO toca la señal.
    """
    shown = {r["ticker"] for r in leaders}
    # Ordenar por fuerza RECIENTE (10d), no por momentum 6m — eso es lo que define un breakout
    # "nuevo" (caso SNOW: 6m flojo pero +58% reciente). Sin ret_10d, cae al fondo.
    cands = sorted(
        [(ind.get("ret_10d") if ind.get("ret_10d") is not None else -9, tk)
         for tk, ind in inds.items()
         if ind.get("above_sma") and ind.get("is_breakout")
         and tk not in pp.positions and tk not in shown],
        reverse=True)
    pick = [tk for _, tk in cands[:n]]
    ctx = _news_sector_ctx(pick, sector_mom or {})
    top_set = top_set or set()
    out = []
    for tk in pick:
        ind = inds.get(tk) or {}
        row = {
            "ticker": tk, "rank": None, "in_top": False, "held": False, "source": None,
            "action": None, "is_top": tk in top_set, "is_breakout": True,
            "momentum_pct": round(ind["momentum"] * 100, 1) if ind.get("momentum") is not None else None,
            "recent_pct": round(ind["ret_10d"] * 100, 1) if ind.get("ret_10d") is not None else None,
            "entry": entry_levels(ind) or {},
        }
        row.update(ctx.get(tk) or {})
        out.append(row)
    return out


def _last_bar_maps(data, ref_date):
    """open/high/close/atr% por ticker SOLO si su ultima barra es de ref_date."""
    opn, hi, cl = {}, {}, {}
    for tk, bars in data.items():
        if bars and bars[-1]["t"] == ref_date:
            opn[tk], hi[tk], cl[tk] = bars[-1]["o"], bars[-1]["h"], bars[-1]["c"]
    return opn, hi, cl


# ETFs para clasificar el SUB-régimen cuando el mercado está risk-off (no para tradear).
# DBC = materias primas, TLT = bonos largos. Distinguen bear inflacionario vs deflacionario vs chop.
REGIME_ETFS = ["DBC", "TLT"]


def _classify_regime(qqq, regime_bars):
    """
    Clasifica el régimen de mercado en 4 tipos (validado en research/backtest_marea_*.py).
    El estado risk-on/off sale de la histéresis ±3% de QQQ vs SMA200 (la misma que filtra Marea).
    En risk-off, DBC/TLT distinguen el sub-tipo. Informativo para el header; NO cambia qué tradea
    el sistema (Marea/PED siguen operando solo en risk-on; los shorts/commodities son futuro).
    """
    risk_on = macro_ok(qqq)
    if risk_on:
        return {"state": "risk_on", "key": "alcista", "emoji": "🟢", "color": "green",
                "label": "ALCISTA", "use": "Marea + PED",
                "legend": "Tech sobre su media de 200 días (con histéresis). Operá Marea y PED."}
    dbc = indicators(regime_bars.get("DBC", [])) or {}
    tlt = indicators(regime_bars.get("TLT", [])) or {}
    if tlt.get("above_sma"):
        return {"state": "risk_off", "key": "deflacionario", "emoji": "🔵", "color": "blue",
                "label": "DEFENSIVO · FUGA A CALIDAD", "use": "Marea en pausa",
                "legend": "Acciones bajo su media de 200 y bonos al alza (fuga a calidad). Marea no "
                          "abre nuevas; mantené lo abierto. Oportunidad futura: bonos/oro/shorts."}
    if dbc.get("above_sma"):
        return {"state": "risk_off", "key": "inflacionario", "emoji": "🟠", "color": "orange",
                "label": "DEFENSIVO · INFLACIONARIO", "use": "Marea en pausa",
                "legend": "Acciones bajo su media de 200 y materias primas al alza. Marea no abre "
                          "nuevas; mantené lo abierto. Oportunidad futura: materias primas/energía/shorts."}
    return {"state": "risk_off", "key": "lateral", "emoji": "⚪", "color": "gray",
            "label": "DEFENSIVO · LATERAL", "use": "Esperar (cash)",
            "legend": "Acciones bajo su media de 200 y sin tendencia clara en bonos/commodities "
                      "(chop). Marea no abre nuevas; el cash es el rey."}


def run(send_alert=True):
    uni = load_universe()
    fetch_list = sorted(set(uni) | MEGA_CAP_PED)   # incluir mega-caps PED en el fetch
    data = fetch_daily_batch(fetch_list + ["QQQ"] + SECTOR_ETFS + REGIME_ETFS)
    qqq = data.pop("QQQ", [])
    sector_bars = {e: data.pop(e, []) for e in SECTOR_ETFS}   # ETFs de sector fuera del trading
    regime_bars = {e: data.pop(e, []) for e in REGIME_ETFS}   # ETFs solo para clasificar régimen
    if not qqq:
        print("[pilot] sin datos de QQQ — abortando"); return
    ref_date = qqq[-1]["t"]
    regime = _classify_regime(qqq, regime_bars)

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

    # Días hábiles en cartera por posición (contexto D+x; para PED define la salida ~Day+7).
    held_days = {tk: days_held(data.get(tk, []), p["entry_date"])
                 for tk, p in pp.positions.items()}

    if pp.s["last_run_date"] == ref_date:
        print(f"[pilot] ya corrido para {ref_date} — nada que hacer")
        return _emit_dashboard(pp, inds, cl, qqq, ref_date, [], top_set=top_set,
                               sector_mom=sector_mom, send_alert=False, held_days=held_days,
                               regime=regime)

    actions = []   # fills de hoy (ordenes que estaban pendientes)
    earnings_map = fetch_earnings_map([t for t in MEGA_CAP_PED if t in data])
    # Visibilidad de cobertura: si NINGÚN mega-cap trae earnings en la ventana, la fuente
    # está rota (es justo lo que pasó con yfinance en py3.9). Un día normal sí debe haber
    # algunos (earnings es trimestral pero el universo es grande). 0 = revisar Finnhub.
    _ped_with_earnings = sum(1 for t in MEGA_CAP_PED if earnings_map.get(t))
    _ped_universe = len([t for t in MEGA_CAP_PED if t in data])
    if _ped_universe and _ped_with_earnings == 0:
        print(f"[PED][WARN] 0/{_ped_universe} mega-caps con earnings en la ventana — "
              f"¿fuente Finnhub caída? (con yfinance roto daba exactamente esto)")
    else:
        print(f"[PED] earnings detectados en {_ped_with_earnings}/{_ped_universe} mega-caps")

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
    # Candidatos PED del día (TODOS — se muestran en "Estrategias disponibles" aunque no haya slot libre)
    ped_cands_full = [[tk, round(rx, 1)] for tk, rx in ped_entry_candidates(data, earnings_map, ref_date)
                      if tk not in held and tk in cl]
    if slots_next > 0:
        if macro:   # Marea solo en régimen alcista (QQQ > SMA200)
            cands = [tk for tk in entry_candidates(inds) if tk not in held and tk in cl]
            new_buys = cands[:slots_next]
        # PED usa los slots que Marea no tomó (earnings es idiosincrático, no depende del macro)
        slots_ped = slots_next - len(new_buys)
        if slots_ped > 0:
            ped_cands = [tk for tk, _ in ped_cands_full if tk not in new_buys]
            new_ped_buys = ped_cands[:slots_ped]

    pp.s["pending"] = {"buys": new_buys, "sells": new_sells, "ped_buys": new_ped_buys,
                       "ped_candidates": ped_cands_full}

    # ── 4) marcar equity al cierre + persistir ──────────────────────────────────
    eq = pp.record_equity(ref_date, cl)
    pp.s["last_run_date"] = ref_date
    pp.save()

    # Niveles de entrada sugeridos (informativos) para las COMPRAS de mañana — Fase 1.
    buy_levels = {tk: entry_levels(inds.get(tk)) for tk in (new_buys + new_ped_buys)}
    buy_levels = {k: v for k, v in buy_levels.items() if v}

    _print_console(pp, actions, new_buys, new_sells, new_ped_buys, cl, eq, macro, ref_date, buy_levels)
    return _emit_dashboard(pp, inds, cl, qqq, ref_date, actions,
                           new_buys=new_buys, new_sells=new_sells, new_ped_buys=new_ped_buys,
                           top_set=top_set, sector_mom=sector_mom, macro=macro,
                           buy_levels=buy_levels, send_alert=send_alert, held_days=held_days,
                           regime=regime)


# ── salida: consola, dashboard json, alerta whatsapp ───────────────────────────

def _pos_lines(pp, cl):
    out = []
    for tk, p in pp.positions.items():
        px = cl.get(tk, p["entry_price"])
        pnl = (px/p["entry_price"]-1)*100
        out.append((tk, round(pnl, 1), p["entry_price"], round(px, 2)))
    return sorted(out, key=lambda x: -x[1])


def _live_chandelier(pp, inds, tk):
    """Stop 4×ATR vivo (chandelier) de una posición Marea: hh_desde_entrada − 4×ATR actual.
    Sigue al máximo alcanzado pero se recalcula con el ATR del día: puede BAJAR si la
    volatilidad se expande (no es trailing fijo — el ratchet se midió y pierde, 2026-06-10).
    None para PED (sale por tiempo) o si falta ATR/hh."""
    pos = pp.positions.get(tk, {})
    if pos.get("source", "marea") == "ped":
        return None
    ind = inds.get(tk)
    if not ind or ind.get("atr") is None or not pos.get("hh"):
        return None
    stop = chandelier_stop(pos["hh"], ind["atr"])
    return round(stop, 2) if stop is not None else None


def _levels_lines(tickers, buy_levels, prefix="   ↳ "):
    """Líneas legibles de niveles de entrada sugeridos para los tickers de compra."""
    out = []
    for tk in tickers or []:
        lv = (buy_levels or {}).get(tk)
        if not lv or not lv["levels"]:
            continue
        lvls = "  ".join(f"{lbl} ${p:g}" for lbl, p in lv["levels"])
        line = f"{prefix}{tk}: {lvls}"
        if lv.get("stop"):
            line += f"  | stop~ ${lv['stop']:g}"
        out.append(line)
    return out


def _print_console(pp, actions, buys, sells, ped_buys, cl, eq, macro, date, buy_levels=None):
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
    for ln in _levels_lines((buys or []) + (ped_buys or []), buy_levels):
        print(ln)
    pl = _pos_lines(pp, cl)
    if pl:
        print("En cartera:")
        for tk,pnl,ent,px in pl:
            print(f"  {tk:6} {pnl:+5.1f}%  (entrada ${ent} -> ${px})")


def _build_alert(pp, cl, eq, actions, buys, sells, ped_buys, macro, date, buy_levels=None):
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
        lv_lines = _levels_lines((buys or []) + (ped_buys or []), buy_levels)
        if lv_lines:
            L.append("\n🎯 _Entradas sugeridas (límite, opcional):_")
            L.extend(lv_lines)
    if not macro:
        L.append("\n⚠️ Mercado bajo SMA200 — sin nuevas entradas")
    pl = _pos_lines(pp, cl)
    if pl:
        L.append("\n📊 En cartera:")
        for tk,pnl,ent,px in pl[:8]:
            L.append(f"• {tk} {pnl:+.1f}%")
    return "\n".join(L)


def _emit_dashboard(pp, inds, cl, qqq, date, actions, new_buys=None, new_sells=None,
                    new_ped_buys=None, top_set=None, sector_mom=None, macro=True,
                    buy_levels=None, send_alert=True, held_days=None, regime=None):
    top_set = top_set or set()
    held_days = held_days or {}
    # Niveles de entrada sugeridos. En el camino "ya corrido" no se recomputan compras,
    # así que se derivan de pp.pending con los indicadores frescos de hoy.
    pend_buys = (pp.pending.get("buys") or []) + (pp.pending.get("ped_buys") or [])
    if buy_levels is None:
        buy_levels = {tk: entry_levels(inds.get(tk)) for tk in pend_buys}
        buy_levels = {k: v for k, v in buy_levels.items() if v}
    # Contexto informativo por compra (sector + noticia adversa) — NO veta, solo informa.
    buy_context = _buy_context(pend_buys, sector_mom or {})
    # Top-10 líderes (ranking fuerza relativa) — panel discrecional que reemplaza "Mañana al abrir".
    leaders = _leaders(pp, inds, cl, top_set, sector_mom or {}, held_days, pp.pending)
    # Breakouts nuevos (fuera del top-10 por momentum 6m bajo) — caso SNOW. Informativo.
    fresh_breakouts = _fresh_breakouts(pp, inds, cl, top_set, sector_mom or {}, leaders)
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
    # Señales de estrategia de evento (PED hoy; futuras estrategias se agregan aquí) →
    # panel "Estrategias disponibles". Se muestran AUNQUE no haya slot (señal != compra).
    ped_buy_set = set(pp.pending.get("ped_buys") or [])
    strategy_signals = [
        {"strategy": "PED", "ticker": tk, "conviction": rx,
         "star": rx >= 10.0, "buying": tk in ped_buy_set,
         "note": "Compra Day+2 open, sale ~Day+7"}
        for tk, rx in (pp.pending.get("ped_candidates") or [])
    ]
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
        "max_positions": K,                                 # nº máximo de posiciones (K) — para el "X/K" del frontend
        "macro_bullish": (regime["state"] == "risk_on") if regime else macro,
        "regime": regime,                                   # 4-régimen para el header (informativo)
        "positions": [
            {"ticker": tk, "pnl_pct": pnl, "entry": ent, "price": px,
             "source": pp.positions.get(tk, {}).get("source", "marea"),
             "entry_date": pp.positions.get(tk, {}).get("entry_date"),
             "days_held": held_days.get(tk),
             "ped_hold_days": PED_HOLD_DAYS,
             "stop_4atr": _live_chandelier(pp, inds, tk),   # salida Marea viva (None en PED)
             "top": tk in top_set}
            for tk, pnl, ent, px in _pos_lines(pp, cl)
        ],
        "pending": pp.pending,
        "entry_levels": buy_levels,                         # niveles sugeridos por ticker de compra (Fase 1)
        "buy_context": buy_context,                         # contexto informativo (sector + noticia adversa)
        "leaders": leaders,                                 # top-10 ranking fuerza relativa (panel discrecional)
        "fresh_breakouts": fresh_breakouts,                 # breakouts nuevos invisibles en el top-10 (caso SNOW)
        "strategy_signals": strategy_signals,               # panel "Estrategias disponibles" (PED + futuras)
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
                            macro, date, buy_levels)
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
