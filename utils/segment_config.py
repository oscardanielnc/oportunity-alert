"""
Configuracion centralizada de segmentos, time-stops y thresholds.
Importado por api/app.py (watcher live) y backtest_complete.py (backtest fiel).
Cualquier cambio aqui se refleja en AMBOS automaticamente.
"""

# ── Time-stop globales (defaults, pueden ser sobreescritos por SEGMENT_PARAMS) ──
TIMESTOP_SCANS       = {"1m": 20, "5m": 12, "15m": 4}
TIMESTOP_ADVERSE_PCT = {"1m": 0.5, "5m": 0.0, "15m": 1.0}

# ── Session thresholds ────────────────────────────────────────────────────────
# Key "default" se usa cuando ningun otro aplica.
# Lima hours (UTC-5). session: "PRE_MARKET" | "REGULAR" | None (cualquiera)
SESSION_THRESHOLDS = {
    "pre_market_early": {
        "session":    "PRE_MARKET",
        "hour_start": 3,
        "hour_end":   8,
        "threshold":  6,
        "label":      "Pre-market temprano 03-08h",
    },
    "market_close": {
        "session":    "REGULAR",
        "hour_start": 15,
        "hour_end":   17,
        "threshold":  6,
        "label":      "Cierre mercado 15-17h",
    },
    "default": {
        "threshold":  5,
        "label":      "Estandar",
    },
}

# ── Ticker thresholds (hard floor sobre segment params) ───────────────────────
TICKER_THRESHOLDS: dict = {
    "RDDT": 7,
}

# ── Ticker → segmento ─────────────────────────────────────────────────────────
TICKER_SEGMENT: dict = {
    # LARGE_CAP: alta liquidez, ATR moderado, patrones fiables
    "NVDA": "LARGE_CAP", "AMD":  "LARGE_CAP", "AVGO": "LARGE_CAP",
    "MU":   "LARGE_CAP", "MSFT": "LARGE_CAP", "INTC": "LARGE_CAP",
    "ARM":  "LARGE_CAP", "MRVL": "LARGE_CAP", "ANET": "LARGE_CAP",
    "VRT":  "LARGE_CAP", "SMCI": "LARGE_CAP", "SNDK": "LARGE_CAP",
    "LITE": "LARGE_CAP", "COHR": "LARGE_CAP", "WOLF": "LARGE_CAP",
    # QUANTUM: volatilidad extrema, NO operar en 5m
    "IONQ": "QUANTUM", "QBTS": "QUANTUM",
    "RGTI": "QUANTUM", "QUBT": "QUANTUM",
    # AI_SW: AI/software/social, confirm_req=3 clave
    "PLTR": "AI_SW", "APP":  "AI_SW", "RDDT": "AI_SW",
    "SOUN": "AI_SW", "CRWD": "AI_SW", "SHOP": "AI_SW",
    "BBAI": "AI_SW", "INOD": "AI_SW", "APLD": "AI_SW",
    "HOOD": "AI_SW", "SOFI": "AI_SW",
}

# ── Parametros optimos por (segmento, tf) — backtest grid 2026-05-28 ──────────
# threshold_adj : delta al umbral base de entrada (5 neutral / 4 bullish HTF)
# n_binary_min  : minimo de criterios L1-L6/S1-S6 activos (sin patrones)
# pattern_cap   : limite al puntaje de patrones (2=cap DNA, 4=sin limite)
# confirm_req   : scans consecutivos necesarios para confirmar entrada
# max_scans     : time-stop en scans (None = usar TIMESTOP_SCANS global)
# adverse_pct   : adversidad minima (None = usar TIMESTOP_ADVERSE_PCT global)
# block_new     : True = no abrir nuevas posiciones en este TF
# block_short   : True = bloquear ENTRAR_SHORT en este segmento+TF
SEGMENT_PARAMS: dict = {
    # LARGE_CAP — confirm_req=3 en 1m mejora WR. threshold_adj=0 en todos
    # (el +1 en 15m fue validado SIN session thresholds; combinado = sobre-filtrado)
    ("LARGE_CAP", "1m"):  {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":2,   "max_scans":15,  "adverse_pct":0.7,
                            "block_new":False, "block_short":False},
    ("LARGE_CAP", "5m"):  {"threshold_adj":0, "n_binary_min":3, "pattern_cap":2,
                            "confirm_req":2,   "max_scans":16,  "adverse_pct":0.5,
                            "block_new":False, "block_short":False},
    ("LARGE_CAP", "15m"): {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":2,   "max_scans":4,   "adverse_pct":1.0,
                            "block_new":False, "block_short":True},  # SHORT PF=0.61

    # QUANTUM — 5m NO OPERAR (max PF 0.44 en 324 combos, estructuralmente no-rentable)
    # confirm_req reducido a 2 en 15m (era 3 — demasiado restrictivo en combinacion)
    ("QUANTUM", "1m"):    {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                            "confirm_req":2,   "max_scans":15,  "adverse_pct":0.3,
                            "block_new":False, "block_short":False},
    ("QUANTUM", "5m"):    {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":2,   "max_scans":12,  "adverse_pct":0.3,
                            "block_new":True,  "block_short":False},
    ("QUANTUM", "15m"):   {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":2,   "max_scans":4,   "adverse_pct":1.0,
                            "block_new":False, "block_short":True},  # SHORT PF=0.29

    # AI_SW — confirm_req=3 en 5m y 15m; reducido a 2 donde resultaba en 0 trades
    ("AI_SW", "1m"):      {"threshold_adj":0, "n_binary_min":2, "pattern_cap":2,
                            "confirm_req":2,   "max_scans":15,  "adverse_pct":0.5,
                            "block_new":False, "block_short":False},
    ("AI_SW", "5m"):      {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                            "confirm_req":3,   "max_scans":8,   "adverse_pct":0.0,
                            "block_new":False, "block_short":True},
    ("AI_SW", "15m"):     {"threshold_adj":0, "n_binary_min":1, "pattern_cap":4,
                            "confirm_req":3,   "max_scans":3,   "adverse_pct":1.0,
                            "block_new":False, "block_short":False},
}

SEG_DEFAULT: dict = {
    "threshold_adj":0, "n_binary_min":2, "pattern_cap":2,
    "confirm_req":2,   "max_scans":None, "adverse_pct":None,
    "block_new":False, "block_short":False,
}

# ── TICKER_PARAMS — parametros individuales por ticker x TF ──────────────────
# Generados por backtest_perticker.py (2026-05-28).
# Criterio de validez: min 15 trades en backtest.
# "USE_SEGMENT": ticker cae al segmento (pocos datos para calibracion individual).
# Jerarquia de precedencia: TICKER_PARAMS > SEGMENT_PARAMS > SEG_DEFAULT
TICKER_PARAMS: dict = {
    # NVDA [LARGE_CAP] — 1m igual que segmento; 5m degradaria (n insuficiente)
    ("NVDA", "1m"):  {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                      "confirm_req":2,   "max_scans":15,   "adverse_pct":0.7,
                      "block_new":False, "block_short":False},

    # PLTR [AI_SW] — 5m: PF 0.71 → 2.60 con nb=3, adv=0.0
    ("PLTR", "5m"):  {"threshold_adj":0, "n_binary_min":3, "pattern_cap":2,
                      "confirm_req":2,   "max_scans":12,   "adverse_pct":0.0,
                      "block_new":False, "block_short":True},

    # AVGO [LARGE_CAP] — 5m: mejora marginal PF 2.33 → 2.53
    ("AVGO", "5m"):  {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                      "confirm_req":2,   "max_scans":12,   "adverse_pct":0.3,
                      "block_new":False, "block_short":False},

    # MSFT [LARGE_CAP] — 1m BLOQUEADO: PF 0.23 en 324 combos, estructuralmente malo
    #                    5m: PF 1.03 → 1.91 con adv=0.0
    ("MSFT", "1m"):  {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                      "confirm_req":2,   "max_scans":15,   "adverse_pct":0.7,
                      "block_new":True,  "block_short":False},
    ("MSFT", "5m"):  {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                      "confirm_req":2,   "max_scans":12,   "adverse_pct":0.0,
                      "block_new":False, "block_short":False},
    ("MSFT", "15m"): {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                      "confirm_req":2,   "max_scans":3,    "adverse_pct":0.5,
                      "block_new":False, "block_short":True},
}
# Tickers con USE_SEGMENT (pocos datos para calibracion individual):
# AMD, MU, IONQ, QBTS, APP, RDDT — usan SEGMENT_PARAMS como fallback


def get_segment_params(ticker: str, interval: str) -> dict:
    """
    Jerarquia: TICKER_PARAMS > SEGMENT_PARAMS > SEG_DEFAULT
    Permite calibracion individual donde hay datos suficientes,
    y cae al segmento o default cuando no los hay.
    """
    # 1. Params especificos del ticker (mayor precision, min 15 trades validado)
    if (ticker, interval) in TICKER_PARAMS:
        return TICKER_PARAMS[(ticker, interval)]
    # 2. Params del segmento (fallback cuando no hay datos suficientes)
    seg = TICKER_SEGMENT.get(ticker)
    if seg:
        return SEGMENT_PARAMS.get((seg, interval), SEG_DEFAULT)
    # 3. Default global
    return SEG_DEFAULT


def get_session_threshold_for_hour(lima_hour: int, is_regular: bool,
                                    interval: str = "1m") -> int:
    """
    Devuelve el threshold de entrada segun hora Lima y si es sesion regular.
    interval: el session threshold aplica con mas fuerza en 1m (mas ruido)
    que en 5m/15m donde las velas son mas estables.
    Para 15m: no aplica pre-market filter (velas de 15min ya filtran ruido).
    """
    # 15m: solo aplica market_close, no pre-market (velas ya son robustas)
    if interval == "15m":
        if is_regular and 15 <= lima_hour < 17:
            return SESSION_THRESHOLDS["market_close"]["threshold"]
        return SESSION_THRESHOLDS["default"]["threshold"]

    for key, cfg in SESSION_THRESHOLDS.items():
        if key == "default":
            continue
        req_session = cfg.get("session")
        if req_session == "PRE_MARKET" and is_regular:
            continue
        if req_session == "REGULAR" and not is_regular:
            continue
        h_start = cfg.get("hour_start", 0)
        h_end   = cfg.get("hour_end",   24)
        if h_start <= lima_hour < h_end:
            return cfg["threshold"]
    return SESSION_THRESHOLDS["default"]["threshold"]
