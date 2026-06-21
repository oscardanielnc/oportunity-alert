# REESTRUCTURACIÓN — 3 brazos por TIPO de señal
# Estado: 2026-06-20 | Dueño: Oscar Navarro | Asistente: Claude
# Nace del estudio inverso de saltos >=12% (ver NEWS_REACTION_PLAN.md + memorias
# news-reaction-findings / news-reaction-measurement).

## DIAGNÓSTICO QUE LO MOTIVA (datos, no opinión)
En 25 días hubo **106 saltos >= 12% anormal en <=4h** (oportunidad real: DELL +40%, SNOW +38%,
QBTS/RGTI +25%…). El sistema **perdió el 88%** y de los que vio llegó **tarde** (lead negativo).
Autopsia de los 94 misses:
- **~9 EARNINGS** (calendario-conocible) — los más limpios y grandes.
- **~12-15 NOTICIA real no surfaceada** (gobierno/quantum, contratos, leases, ofertas, MOU,
  endorsements) — llegaron por **Benzinga real-time / SeekingAlpha que YA TENEMOS gratis**; el
  fallo fue COBERTURA (no monitoreábamos el ticker), NO falta de fuente.
- **~57 MOMENTUM sin noticia** — small-caps hipervolátiles (WOLF/LUNR/APPS/AMC/QUBT).

→ Cada tipo de señal es un BRAZO distinto. Se ejecutan en 3 ventanas con prioridad:
**Binance perp (24/7, apalancado) > eToro 24/5 > horario de mercado** (según dónde cotice el ticker).

---

## BRAZO 1 — NOTICIAS (EN CURSO — terminándolo ahora)
Catalizador discreto fresco → entrada rápida. Lo que ya sabemos:
- El score de convicción NO calibra; el edge está en CATEGORÍA + FRESCURA + VELOCIDAD, no en el score.
- Backbone de velocidad = **ALPACA_BENZINGA (WebSocket, age 0) + SEC_EDGAR (age 0)**. Gratis y suficiente.
- Yahoo/Finnhub-batch = lag 106-474min → **deprior­izar/quitar** (no aportan velocidad, solo ruido).
- El cuello de botella es COBERTURA: ampliar el universo monitoreado captura la Benzinga que ya recibimos.

### Acciones (todas gratis):
- [x] Kill 8-K Item 5.07 (votación) sin item material; degrade PT-raise "Maintains" (keyword_filter).
- [ ] Deprior­izar/quitar fuentes laggeadas (FINNHUB_YAHOO, FINNHUB_BENZINGA/CNBC batch); quedarse con
      ALPACA_BENZINGA + EDGAR como backbone.
- [ ] Ampliar universo monitoreado a movers volátiles: WOLF, FLY, LUNR, APLD, INOD, SMCI, AMC, BB,
      BBAI, SOFI, APPS (quantum RGTI/IONQ/QBTS/QUBT ya están).
- [ ] Reforzar keywords categorías cubeta-2: PROSPECTUS, FILES FOR OFFERING, LEASE AGREEMENT,
      MEMORANDUM OF UNDERSTANDING, GOVERNMENT STAKE; arreglar los 6 catalizadores mal filtrados.
- [ ] (Forward) medir lead time en vivo tras los cambios; si seguimos tarde en cat. reales → recién
      evaluar Benzinga Pro (NO un tier de Finnhub).
- [ ] Commit + deploy a la VM de los cambios de filtro ya hechos.

---

## BRAZO 2 — EARNINGS (📌 PENDIENTE — documentado, no iniciado)
Los movimientos más limpios y grandes (DELL +40%, SNOW +38%) son EARNINGS, **conocibles por
calendario con días de antelación**. NO necesitan feed rápido — necesitan una jugada para el gap.
- Reusar/extender el motor **PED (Post-Earnings Drift) del Piloto**, ya validado y existente.
- Insumo: `utils/earnings_calendar.py` (Finnhub) — extender a fechas futuras + pre-posicionamiento.
- Decisión abierta: ¿jugar el gap intradía (backtest dice net-negativo) o solo el PED multi-día?
- Ejecución: DELL/SNOW no tienen perp Binance → eToro 24/5 / horario.
- **No iniciar hasta cerrar el brazo Noticias.**

---

## BRAZO 3 — MOMENTUM (📌 PENDIENTE — documentado, no iniciado)
El grueso del volumen perdido (~57 de 94) son small-caps que saltan 12%+ **sin noticia discreta**
(flujo/squeeze/simpatía: WOLF, LUNR, APPS, AMC, QUBT…). No es brazo de noticias — es **señal de
PRECIO** (RVOL/breakout intradía), sin IA.
- Construir un scanner de momentum intradía (anormal vs QQQ + RVOL) sobre el universo volátil.
- Ejecución 24/5 en eToro o intradía; casi ninguno tiene perp Binance.
- Ojo: alto ruido — exige validación de edge neto-de-fees antes de operar (lección Watcher/pre-market).
- **No iniciar hasta cerrar el brazo Noticias.**

### VALIDACIÓN 1 (hecho 2026-06-20, `research/momentum_backtest.py`, 67 tickers, fee_rt 0.2%):
- Saltos **chicos (2% anormal/30m) = SIN edge** (fwd neto ~0/negativo, win 44-49%) — ruido.
- Saltos **grandes (>=5% anormal + RVOL>=2) = SÍ continúan**: fwd neto **+1.0% en 1-2h, win ~60%**,
  exceso vs QQQ ~+1% (edge real, no beta). UP y DOWN. La tesis "los grandes siguen" se sostiene.
- ⚠️ Muestra chica (n~50-70), un régimen. El edge (~1%) es delgado vs costo de ejecución real
  (spreads eToro en small-caps pueden comérselo). **Make-or-break = venue/liquidez.**
- PENDIENTE antes de construir: (a) más historia/muestra + OOS, (b) spread real por ticker,
  (c) cuáles están en Binance perp (fee ~0.05%) para rutear ahí; (d) sensibilidad al fee.

### VALIDACIÓN 2 — ⛔ VEREDICTO: ARCHIVAR la versión naïve (`research/momentum_validate2.py`
### + `extend_bars_history.py`, muestra ampliada a Feb-Jun 2026, n=463 en >=5%):
- El +1%/60% de junio era espejismo de muestra chica + régimen. Con 4.5 meses el edge >=5% cae a
  **+0.43%/55% a fee Binance (0.05%), BREAKEVEN/negativo a fee eToro (0.5%)**, win 53-55%.
- THR 4% = sin edge (win 49%). El poco edge positivo está **concentrado en 3 nombres** (INOD/LUNR/
  APPS); FLY/AMC/IONQ negativos → quitando outliers queda plano. La mayoría de los movers NO están
  en Binance perp (único venue con fee bajo donde sobreviviría).
- **Misma lección que Watcher/pre-market: momentum naïve en small-caps = marginal/negativo tras fees.**
  NO construir. Solo sobreviviría como refinamiento per-ticker en perp = sobreajuste sobre muestra fina.
- → El brazo Momentum queda ARCHIVADO. Prioridad tradeable pasa a EARNINGS (PED, ya validado).

---

## SCORE DE ACCIÓN (reemplaza el score de convicción — `utils/signal_score.py`)
El score de convicción (0-7) NO predice la reacción → se deja de MOSTRAR y de DECIDIR (confunde).
Se REUTILIZA el campo `conviction_score` con un nuevo cálculo 0-10:
`score = categoria(0-6) + frescura(0-2) + confirmacion_precio(0-2)`.
- categoria: contrato/gobierno/FDA=6, oferta/lease/partnership=5, M&A=4, earnings/upgrade-real/regFD=3,
  8-K/otro=2, sector=1, PT-maintains/votación=0 (ya filtrados).
- frescura: age<=15min=2, <=60=1, >60=0.  confirmacion: precio ya se mueve en la dirección=2/1/0.
Cero cambio de esquema; solo cambia quién alimenta el campo. PENDIENTE: cablear en `main.process_article`
(que el `conviction_score` salga de aquí) + ordenar dashboard por este score + quitar el viejo de la UI.

## REGLA DE SMS (qué se envía)
Decisión Oscar: enviar todo lo bueno; suprimir lo de <1% de impacto esperado. Como aún no se puede
predecir el % por-noticia (lo aprenderá el scoreboard forward), se aproxima por categoría: las de
impacto ~0 (PT-maintains, sector, votación) puntúan bajo y no pasan el umbral. `should_send_sms` =
score >= 6. Cuando el scoreboard acumule impacto real por categoría, el umbral pasa a "MFE esperado
>= 1%" (data-driven, no adivinado).

## BRAZO DE AUDITORÍA (scoreboard permanente — reconvertir la sección de Trades)
La sección de trades pasa de "auditar noticias (one-shot)" a **scoreboard permanente de los 3 brazos**:
cada señal (noticia/earnings/momentum) se mide con la maquinaria ya construida (retorno anormal +
MFE/MAE + lead time), etiquetada por brazo + categoría. Es el libro mayor de "¿funcionó?" que habilita
operar con plata real y, a futuro, auto-operar. = Fase 1 (forward) del NEWS_REACTION_PLAN, generalizada.

## HIPÓTESIS APARCADA — Reversión (NO perseguir ahora)
Idea (Oscar): entrar cuando el precio empieza a REVERTIR tras un salto. Análisis: invertir el
trigger de momentum es el ESPEJO de lo ya medido (~0 neto de fees) → no hay edge. Una reversión
real necesita señal propia de AGOTAMIENTO (RSI/Bollinger extremos, clímax de volumen, estiramiento
vs VWAP) + esperar confirmación del giro (no entrar en el pico). Es la misma familia de scalping
intradía net-negativa (Watcher/pre-market/momentum) + mismo problema fees/venue, y es dominio de
precio. DECISIÓN: documentada, NO se construye ahora. Si se retoma un brazo de precio, testear con
señales de agotamiento propias y disciplina neto-de-fees.

## TRUMP TRACKER — unificar MEDICIÓN, conservar captura especial (en evaluación)
El tracker ya reutiliza los feeds y clasifica con IA barata {impacto, alcance, tickers_afectados};
hace el mapeo uno-a-muchos (macro→cesta) que el brazo per-ticker NO. Decisión: NO disolverlo en el
filtro per-ticker (perdería la cobertura macro), SÍ medir su impacto con la misma maquinaria (retorno
anormal + frescura + ¿los tickers afectados se movieron?) y meterlo al mismo scoreboard. Pendiente:
(1) medir impacto real (trae trump_feed.json de la VM); (2) si predice bien → Truth Social como fuente
fresca (postea primero = age 0, el lever que probamos que importa) + unificar al scoreboard. Caso
fuerte: "Trump Admin To Take Quantum Stakes" movió RGTI/IONQ/QBTS +25%.

## PLAN — MARKET MOVERS multi-fuente + SCOREBOARD como validador (2026-06-20)

### Cómo se mide una declaración que mueve VARIOS tickers (aclarado)
UNA señal POR ticker (no una sola). Radar = 1 tarjeta (la declaración + chips de tickers);
scoreboard = N señales medidas independientes. Venue de medición DUAL: perp (24/7, capta overnight)
si el ticker tiene perp; si no, Alpaca anclado al PRÓXIMO OPEN (`_anchor`). Ningún ticker queda sin
medir. Caso real validado: post Trump→INTC +5.5% (perp, overnight); WOLF overnight→next-open −18.75%.

### Fase 0 — HECHO Y EN VIVO (recolectando ya)
Scoreboard mide noticias + Market Movers(Trump) a 48h, precio dual, ancla next-open, fuente
etiquetada. Tabla `signal_outcomes`, resolver horario, `/api/scoreboard`, panel + pestaña.

### Fase 1 — Añadir fuentes oficiales (PENDIENTE — lo siguiente)
Generalizar el tracker (arm 'trump'→'market_movers'; `source` distingue). Cada fuente oficial = un
conector como Truth Social (RSS, py3.9, sin Cloudflare, gratis), feed → gate relevancia → misma
IA-clasificación (impacto, tickers) → scoreboard con su source:
- **Fed**: `federalreserve.gov` press releases RSS (FOMC, discursos, política monetaria).
- **Treasury**: `home.treasury.gov` press releases RSS (sanciones, deuda, aranceles).
- **SEC**: `sec.gov` press releases + litigation RSS (+ EDGAR ya lo tenemos para filings).
Validar por fuente: ¿publican PRIMERO en su sitio (lead time) vs wires? ¿qué tipo de comunicado
mueve qué tickers? (lo dice el scoreboard).

### Fase 2 — Loop de validación/afinado (el propósito del scoreboard)
- Panel con desglose por **fuente + categoría** (qué fuente/categoría mueve, cuál ignorar).
- Usar los datos acumulados para **afinar filtros**: suprimir fuentes/categorías que no mueven
  (<1% impacto), promover las que sí. Revisión semanal.
- Cuando haya muestra, el umbral SMS pasa de "score>=6" a "impacto esperado por categoría/fuente >=1%".

## RUTEO A VENTANAS DE EJECUCIÓN (los 3 brazos comparten esto)
Por cada señal, elegir venue en este orden de prioridad:
1. **Binance perp** si el ticker existe como `{T}/USDT:USDT` (24/7, apalancado, fees bajos).
   Cobertura actual (2026-06-20, 15/31): AMD ARM ASML AVGO BE COHR CRM CVX IBM MSFT MU NVDA PLTR TSM UBER.
2. **eToro 24/5** si no hay perp (cubre noches/fines de semana parciales).
3. **Horario de mercado** si tampoco está en eToro 24/5 (solo entrar en sesión operable).
