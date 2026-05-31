"""
Tests unitarios de utils/position_strategy (sin red — mockean las barras Alpaca).

Cubren la lógica de salida-por-estrategia y la atribución híbrida, que es el código
con riesgo de plata (cortar o no un ganador). Correr: python test_position_strategy.py

Output 100% ASCII (cp1252 Windows). No requiere pytest.
"""
import sys
from datetime import date, timedelta

import utils.position_strategy as ps

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def mk_bars(closes, highs=None, start="2026-01-02"):
    d = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c
        out.append({"t": (d + timedelta(days=i)).isoformat(),
                    "o": c, "h": h, "l": min(c, h), "c": c, "v": 1000})
    return out


def test_resolve_hybrid():
    print("resolve_strategy (híbrida):")
    # override gana
    ps.set_tag("FAKE1", "ped")
    try:
        r = ps.resolve_strategy("FAKE1")
        check("override gana -> ped/override", r["strategy"] == "ped" and r["origin"] == "override")
    finally:
        ps.clear_tag("FAKE1")
    # auto-match con piloto (AAOI está en pilot_state.json)
    r2 = ps.resolve_strategy("AAOI")
    check("AAOI auto-match piloto -> marea/pilot", r2["strategy"] == "marea" and r2["origin"] == "pilot")
    # sin match -> manual/default
    r3 = ps.resolve_strategy("NOPEXX")
    check("desconocido -> manual/default", r3["strategy"] == "manual" and r3["origin"] == "default")


def test_chandelier(monkey):
    print("evaluate_exit marea (chandelier):")
    # Pico alto antiguo (h=200) + cierres recientes calmos ~100 -> cierre << stop -> VENDER
    closes = [100] * 28
    highs = list(closes); highs[2] = 200
    monkey(mk_bars(closes, highs))
    ev = ps.evaluate_exit("X", "marea", "2026-01-02")
    check("pico 200 + cierre 100 -> exit True", ev["exit"] and ev["reason"] == "chandelier")

    # Cierres oscilando cerca del máximo -> dentro del stop -> mantener
    closes2 = [100 + (i % 3) for i in range(28)]
    monkey(mk_bars(closes2))
    ev2 = ps.evaluate_exit("X", "marea", "2026-01-02")
    check("cierre cerca del pico -> exit False", (not ev2["exit"]) and ev2["reason"] == "chandelier_ok")
    check("stop_price calculado", ev2["stop_price"] is not None)


def test_ped(monkey):
    print("evaluate_exit ped (tiempo):")
    bars = mk_bars([100] * 10)
    monkey(bars)
    ev = ps.evaluate_exit("X", "ped", bars[0]["t"])     # entrada vieja -> hold cumplido
    check("entrada vieja -> exit True (ped_time)", ev["exit"] and ev["reason"] == "ped_time")
    ev2 = ps.evaluate_exit("X", "ped", bars[-1]["t"])   # entrada hoy -> en curso
    check("entrada reciente -> exit False (ped_hold)", (not ev2["exit"]) and ev2["reason"] == "ped_hold")


def test_manual_no_fetch():
    print("evaluate_exit manual (no toca Alpaca):")
    called = {"n": 0}
    orig = ps._get_bars
    ps._get_bars = lambda tk: (called.__setitem__("n", called["n"] + 1) or [])
    try:
        ev = ps.evaluate_exit("X", "manual", "2026-01-02")
        check("manual -> exit False", not ev["exit"])
        check("manual -> NO llama a _get_bars", called["n"] == 0)
    finally:
        ps._get_bars = orig


def test_account_cache():
    print("account cache (atómico):")
    existed = ps.ACCOUNT_CACHE.exists()
    backup = ps.ACCOUNT_CACHE.read_text(encoding="utf-8") if existed else None
    try:
        ps.write_account_cache(5000.0, 6000.0, {"X": {"strategy": "marea", "exit": False}})
        c = ps.read_account_cache()
        check("cash persistido", c.get("available_cash") == 5000.0)
        check("evals persistidos", c.get("evals", {}).get("X", {}).get("strategy") == "marea")
        age = ps.account_cache_age_minutes()
        check("age fresco (<1 min)", age is not None and age < 1)
    finally:
        if backup is not None:
            ps.ACCOUNT_CACHE.write_text(backup, encoding="utf-8")
        elif ps.ACCOUNT_CACHE.exists():
            ps.ACCOUNT_CACHE.unlink()


def main():
    print("=" * 56)
    print("  TESTS — utils/position_strategy")
    print("=" * 56)

    def monkey(bars):
        ps._get_bars = lambda tk: bars

    orig_get_bars = ps._get_bars
    try:
        test_resolve_hybrid()
        test_chandelier(monkey)
        test_ped(monkey)
    finally:
        ps._get_bars = orig_get_bars
    test_manual_no_fetch()
    test_account_cache()

    print("-" * 56)
    if _fails:
        print(f"  {len(_fails)} TEST(S) FALLARON: {_fails}")
        return 1
    print("  TODOS LOS TESTS PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
