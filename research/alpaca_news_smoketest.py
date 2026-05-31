"""
Smoke test del stream de noticias Alpaca: conecta, autentica, se suscribe y
escucha ~20s. Confirma handshake (lo importante) y muestra cualquier noticia
que llegue. Uso: python -m research.alpaca_news_smoketest
"""
import logging
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from sources.alpaca_news import stream_news

WATCHLIST = ["NVDA", "TSM", "QBTS", "IONQ", "RGTI", "QUBT", "PLTR", "APP",
             "AVGO", "AMD", "ASML", "ARM", "RDDT", "CRM", "MSFT", "GOOG",
             "CCJ", "TLN", "BE", "EME", "NOC", "RTX", "LMT", "KTOS", "AVAV",
             "UBER", "SHOP", "MU", "COHR", "IBM", "XOM", "CVX"]

count = {"n": 0}


def on_article(a):
    count["n"] += 1
    print(f"  >> NOTICIA: {a['tickers_found']} [age {a['age_minutes']}m] {a['title'][:60]}")


stop = threading.Event()
t = threading.Thread(target=stream_news, args=(WATCHLIST, on_article, stop), daemon=True)
t.start()

print("Escuchando 20s (si conecta y autentica, el smoke test pasa)...")
time.sleep(20)
stop.set()
time.sleep(2)
print(f"\nFIN. Noticias recibidas en la ventana: {count['n']}")
print("(0 es normal un domingo pre-market; lo que importa es ver 'conectado y suscrito' arriba)")
