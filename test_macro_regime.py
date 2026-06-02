#!/usr/bin/env python3
"""
Test de robustez del régimen macro con HISTÉRESIS (pilot/momentum_signals.macro_ok).
Sin red: usa series sintéticas + el QQQ real cacheado en research/_selection_cache_2020.json.
Corre: python test_macro_regime.py   (solo ASCII en consola, Windows-safe)
"""
import os, json, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pilot.momentum_signals import macro_ok, REGIME, REGIME_HYSTERESIS

CACHE = os.path.join(os.path.dirname(__file__), "research", "_selection_cache_2020.json")
_passed = 0


def ok(cond, msg):
    global _passed
    assert cond, "FALLO: " + msg
    _passed += 1
    print("  PASS:", msg)


def bars(closes):
    return [{"t": f"d{i}", "o": c, "h": c, "l": c, "c": c} for i, c in enumerate(closes)]


# --- referencia independiente de la histéresis (para chequear consistencia) ---
def ref_state(closes, band):
    state = True
    for i in range(REGIME - 1, len(closes)):
        sma = sum(closes[i - REGIME + 1:i + 1]) / REGIME
        px = closes[i]
        if px > sma * (1 + band):
            state = True
        elif px < sma * (1 - band):
            state = False
    return state


def count_flips(closes, hyst):
    """Cambios de estado a lo largo de la serie: binario (hyst=0) vs histéresis."""
    state, flips = True, 0
    for i in range(REGIME - 1, len(closes)):
        sma = sum(closes[i - REGIME + 1:i + 1]) / REGIME
        px = closes[i]; new = state
        if hyst == 0:
            new = px > sma
        else:
            if px > sma * (1 + hyst): new = True
            elif px < sma * (1 - hyst): new = False
        if new != state: flips += 1
        state = new
    return flips


def main():
    print("\n== Test régimen macro con histéresis ==\n")
    band = REGIME_HYSTERESIS
    ok(abs(band - 0.03) < 1e-9, f"banda de histéresis = 3% (es {band})")

    # 1) contrato básico / fallback sin datos
    ok(macro_ok([]) is True, "sin datos -> no bloquea (True)")
    ok(macro_ok(bars([100] * (REGIME - 5))) is True, "datos insuficientes (<REGIME+1) -> True")

    # 2) precio muy por encima de su SMA -> risk-on
    up = bars([100] * REGIME + [130] * 10)          # tail +30% sobre la media
    ok(macro_ok(up) is True, "precio >> SMA200 -> risk-on (True)")

    # 3) precio muy por debajo -> risk-off
    down = bars([100] * REGIME + [70] * 10)         # tail -30%
    ok(macro_ok(down) is False, "precio << SMA200 -> risk-off (False)")

    # 4) HISTÉRESIS: tras caer a risk-off, volver SOLO dentro de la banda NO re-enciende
    #    baja fuerte (risk-off), luego sube apenas por encima de la SMA pero dentro del +3%
    closes = [100] * REGIME + [70] * 40             # fija risk-off
    # ahora la SMA ~ menor; ponemos precio levemente por encima de la SMA pero < SMA*1.03
    seq = [100] * REGIME + [70] * 40
    smatail = sum(seq[-REGIME:]) / REGIME
    seq += [smatail * 1.01] * 5                      # dentro de la banda (+1% < +3%)
    ok(macro_ok(bars(seq)) is False, "dentro de la banda tras risk-off -> sigue risk-off (pegajoso)")
    # y si supera el +3% SÍ re-enciende
    seq2 = [100] * REGIME + [70] * 40
    smatail2 = sum(seq2[-REGIME:]) / REGIME
    seq2 += [smatail2 * 1.05] * 5                    # +5% > +3%
    ok(macro_ok(bars(seq2)) is True, "supera SMA200*(1+3%) -> re-enciende risk-on")

    # 5) banda simétrica: risk-on que cae apenas bajo la SMA (dentro del -3%) NO apaga
    seq3 = [100] * REGIME + [130] * 40              # fija risk-on
    smatail3 = sum(seq3[-REGIME:]) / REGIME
    seq3 += [smatail3 * 0.99] * 5                    # -1% (dentro de -3%)
    ok(macro_ok(bars(seq3)) is True, "dentro de la banda tras risk-on -> sigue risk-on (pegajoso)")

    # 6) CONSISTENCIA con la referencia del backtest sobre QQQ real, en muchos cortes temporales
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE))
        qb = cache.get("QQQ", [])
        closesq = [b["c"] for b in qb]
        ok(len(closesq) > REGIME + 200, f"QQQ cacheado con {len(closesq)} barras")
        mism = 0
        for cut in range(REGIME + 50, len(qb), 25):     # decenas de cortes a lo largo de 6 años
            if macro_ok(qb[:cut]) != ref_state(closesq[:cut], band):
                mism += 1
        ok(mism == 0, f"macro_ok == referencia histéresis en todos los cortes de QQQ ({mism} mismatches)")

        # 7) PROPIEDAD CLAVE: la histéresis hace MENOS flips que el binario (anti-whipsaw)
        fb = count_flips(closesq, 0.0)
        fh = count_flips(closesq, band)
        ok(fh < fb, f"histéresis reduce flips de régimen: binario={fb} -> histéresis={fh}")
    else:
        print("  (omito QQQ real: no existe el cache; corré primero un backtest de research/)")

    print(f"\n== {_passed} checks OK ==\n")


if __name__ == "__main__":
    main()
