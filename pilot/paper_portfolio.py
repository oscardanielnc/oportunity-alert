#!/usr/bin/env python3
"""
Paper portfolio — estado persistente del piloto (sin dinero real).

Semantica identica al backtest: las senales se generan al cierre del dia D
y se EJECUTAN al OPEN del dia D+1 (ordenes pendientes). Esto evita lookahead
y refleja como operaria Oscar (recibe alerta hoy, ejecuta en eToro manana al abrir).

Estado en data/pilot_state.json:
  capital_initial, cash, positions{tk:{...}}, pending{buys,sells},
  closed_trades[], equity_history[{date,equity}], last_run_date
"""
import os, json
from datetime import datetime, timezone

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE_PATH = os.path.join(DATA_DIR, "pilot_state.json")

K          = 8        # maximo de posiciones simultaneas (Estudio H 2026-06-02: K8 mejor DD/retorno
                      # que K5 a igual vol — usar el cash en MÁS nombres, no posiciones más grandes)
VOL_TARGET = 0.03     # objetivo de volatilidad (ATR%) para sizing
FEE_SIDE   = 1.0      # $ por lado (abrir/cerrar)
CAP_INIT   = float(os.environ.get("PILOT_CAPITAL", "10000"))


class PaperPortfolio:
    def __init__(self, state=None):
        self.s = state or {
            "capital_initial": CAP_INIT,
            "cash": CAP_INIT,
            "positions": {},                 # tk -> {entry_date, entry_price, shares, hh}
            "pending": {"buys": [], "sells": []},
            "closed_trades": [],
            "equity_history": [],
            "last_run_date": None,
        }

    # ── persistencia ────────────────────────────────────────────────────────
    @classmethod
    def load(cls):
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH) as f:
                return cls(json.load(f))
        return cls()

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(self.s, f, indent=2)

    # ── operaciones ───────────────────────────────────────────────────────────
    def equity(self, close_by_ticker):
        val = self.s["cash"]
        for tk, p in self.s["positions"].items():
            px = close_by_ticker.get(tk, p["entry_price"])
            val += p["shares"] * px
        return val

    def open_position(self, tk, fill_price, equity_now, atr_pct, date, source="marea"):
        if tk in self.s["positions"]:
            return None
        base = equity_now / K
        if atr_pct and atr_pct > 0:
            base *= min(1.0, VOL_TARGET / atr_pct)
        target = min(self.s["cash"], base)
        if target <= FEE_SIDE + 1:
            return None
        shares = (target - FEE_SIDE) / fill_price
        self.s["cash"] -= shares * fill_price + FEE_SIDE
        self.s["positions"][tk] = {"entry_date": date, "entry_price": round(fill_price, 4),
                                   "shares": shares, "hh": fill_price, "source": source}
        return {"action": "BUY", "ticker": tk, "price": round(fill_price, 2),
                "shares": round(shares, 4), "cost": round(shares * fill_price, 2),
                "source": source}

    def close_position(self, tk, fill_price, date):
        p = self.s["positions"].pop(tk, None)
        if not p:
            return None
        proceeds = p["shares"] * fill_price - FEE_SIDE
        self.s["cash"] += proceeds
        pnl_pct = (fill_price / p["entry_price"] - 1) * 100
        pnl_usd = p["shares"] * (fill_price - p["entry_price"]) - 2 * FEE_SIDE
        trade = {"ticker": tk, "entry_date": p["entry_date"], "exit_date": date,
                 "entry": p["entry_price"], "exit": round(fill_price, 4),
                 "shares": round(p["shares"], 4), "pnl_pct": round(pnl_pct, 2),
                 "pnl_usd": round(pnl_usd, 2)}
        self.s["closed_trades"].append(trade)
        return {"action": "SELL", "ticker": tk, "price": round(fill_price, 2),
                "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2)}

    def update_high(self, tk, high):
        if tk in self.s["positions"]:
            self.s["positions"][tk]["hh"] = max(self.s["positions"][tk]["hh"], high)

    def record_equity(self, date, close_by_ticker):
        eq = self.equity(close_by_ticker)
        hist = self.s["equity_history"]
        if hist and hist[-1]["date"] == date:
            hist[-1]["equity"] = round(eq, 2)
        else:
            hist.append({"date": date, "equity": round(eq, 2)})
        return eq

    # ── helpers de lectura ─────────────────────────────────────────────────────
    @property
    def positions(self):  return self.s["positions"]
    @property
    def cash(self):       return self.s["cash"]
    @property
    def pending(self):    return self.s["pending"]
    @property
    def free_slots(self): return K - len(self.s["positions"])
