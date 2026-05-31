"""
universe.py — Universos de tickers del sistema (hogar estable).

LARGE_CAP_24X5: large-caps 24/5 en eToro (S&P500/Nasdaq100). Se usa para el badge
"24/5" del frontend y será la base del módulo de momentum multi-día (Marea + PED),
que opera justamente large-caps de calidad.
"""

LARGE_CAP_24X5 = {
    "NVDA", "TSM", "PLTR", "AVGO", "AMD", "ASML", "ARM", "CRM", "MSFT", "GOOG",
    "UBER", "SHOP", "MU", "COHR", "IBM", "XOM", "CVX", "CCJ", "NOC", "RTX",
    "LMT", "APP", "AAPL", "META", "AMZN",
}
