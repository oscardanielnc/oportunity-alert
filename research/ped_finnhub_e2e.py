"""
Test end-to-end de PED con la nueva fuente Finnhub: trae barras reales (Alpaca),
trae earnings reales (Finnhub) y verifica que reaction_today() dispara en el dia
de reaccion correcto. Uso: python -m research.ped_finnhub_e2e
"""
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from pilot.ped_signals import fetch_earnings_map, reaction_today, MEGA_CAP_PED
from pilot.momentum_signals import fetch_daily_batch

# Mega-caps que reportaron en las ultimas ~3 semanas (segun coverage test)
TEST = ["NVDA", "CRM", "COST", "WMT", "HD", "MSFT", "AMD", "ORCL"]

print("1) fetch_earnings_map (Finnhub)...")
em = fetch_earnings_map(TEST, lookback_days=30)
for tk in TEST:
    print(f"   {tk:6} -> {em.get(tk, [])}")

print("\n2) barras diarias (Alpaca)...")
data = fetch_daily_batch(TEST, lookback_days=60)
print(f"   tickers con barras: {sorted(data.keys())}")

print("\n3) reaction_today() en cada dia que tuvo earnings:")
hits = 0
for tk in TEST:
    bars = data.get(tk, [])
    evs = em.get(tk, [])
    if not bars or not evs:
        continue
    dates = [b["t"] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    for E, hour in evs:
        if E not in idx:
            continue
        r_idx = idx[E] if hour == "bmo" else idx[E] + 1
        if r_idx >= len(bars):
            continue
        ref = bars[r_idx]["t"]
        # truncar barras hasta el dia de reaccion para simular ese dia
        sub = bars[: r_idx + 1]
        res = reaction_today(sub, evs, ref)
        if res:
            hits += 1
            tag = "ENTRADA PED" if res["reaction_pct"] >= 5.0 else "no califica (<5%)"
            print(f"   {tk:6} earnings {E} ({hour}) -> reaccion Day+1 {res['reaction_pct']:+.1f}% "
                  f"gap {res['gap']:+.1f}%  [{tag}]")

print(f"\nRESULTADO: reaction_today disparo en {hits} eventos. "
      f"{'OK — la cadena PED funciona con Finnhub.' if hits else 'REVISAR — no detecto reacciones.'}")
