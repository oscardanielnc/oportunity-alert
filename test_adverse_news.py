#!/usr/bin/env python3
"""
Tests Fase 3 — alerta de SALIDA por noticia adversa sobre posición abierta.
Sin red: monkeypatch del account_cache y del envío Twilio. ASCII only (cp1252).

Cubre:
  - _held_tickers() lee bien los tickers del cache (y fail-safe si está viejo).
  - _send_adverse_news_alert() arma el cuerpo correcto.
  - El gating de disparo (replica la condición de process_article): solo dispara con
    direccion=SHORT + en cartera + (score_ia>=7 o event_mode).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import utils.position_strategy as ps
import main

PASS = 0; FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [OK]   {name}")
    else:
        FAIL += 1; print(f"  [FAIL] {name}")


# ── _held_tickers ────────────────────────────────────────────────────────────
def test_held_tickers():
    print("\n_held_tickers():")
    ps.read_account_cache = lambda: {"positions": [{"ticker": "LITE"}, {"ticker": "nvda"}]}
    ps.account_cache_age_minutes = lambda: 5.0
    held = main._held_tickers()
    check("lee y normaliza a mayúsculas", held == {"LITE", "NVDA"})

    ps.account_cache_age_minutes = lambda: 90.0           # cache viejo
    check("fail-safe: cache >60min -> set() vacío", main._held_tickers() == set())

    ps.read_account_cache = lambda: {}                    # sin cache
    ps.account_cache_age_minutes = lambda: None
    check("fail-safe: sin cache -> set() vacío", main._held_tickers() == set())


# ── _send_adverse_news_alert ───────────────────────────────────────────────────
def test_alert_body():
    print("\n_send_adverse_news_alert():")
    sent = {}
    main._send_twilio_raw = lambda body, to: sent.update({"body": body, "to": to}) or True
    main._send_adverse_news_alert("LITE", {"resumen_cataliz": "guidance cut, downgrade"}, "+51999")
    b = sent.get("body", "")
    check("incluye el ticker", "LITE" in b)
    check("marca ADVERSA/URGENTE", "ADVERSA" in b and "URGENTE" in b)
    check("incluye el resumen", "guidance cut" in b)
    check("destinatario correcto", sent.get("to") == "+51999")


# ── gating (replica la condición inline de process_article) ────────────────────
def _would_fire(direction, ticker, held, score_ia, event_mode):
    return (direction == "SHORT" and ticker.upper() in held
            and (score_ia >= main.ADVERSE_EXIT_SCORE_MIN or event_mode))


def test_gating():
    print("\ngating de disparo:")
    held = {"LITE", "NVDA"}
    check("SHORT + en cartera + score 8 -> dispara",
          _would_fire("SHORT", "LITE", held, 8, False))
    check("SHORT + en cartera + event_mode (score bajo) -> dispara",
          _would_fire("SHORT", "LITE", held, 4, True))
    check("SHORT + en cartera + score 5 sin evento -> NO dispara",
          not _would_fire("SHORT", "LITE", held, 5, False))
    check("LONG (alcista) aunque en cartera -> NO dispara",
          not _would_fire("LONG", "LITE", held, 9, True))
    check("SHORT pero NO en cartera -> NO dispara",
          not _would_fire("SHORT", "TSLA", held, 9, True))


if __name__ == "__main__":
    print("=" * 60)
    print("  TESTS Fase 3 — alerta de salida por noticia adversa")
    print("=" * 60)
    test_held_tickers()
    test_alert_body()
    test_gating()
    print(f"\n  {PASS} OK / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)
