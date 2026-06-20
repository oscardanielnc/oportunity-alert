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

---

## RUTEO A VENTANAS DE EJECUCIÓN (los 3 brazos comparten esto)
Por cada señal, elegir venue en este orden de prioridad:
1. **Binance perp** si el ticker existe como `{T}/USDT:USDT` (24/7, apalancado, fees bajos).
   Cobertura actual (2026-06-20, 15/31): AMD ARM ASML AVGO BE COHR CRM CVX IBM MSFT MU NVDA PLTR TSM UBER.
2. **eToro 24/5** si no hay perp (cubre noches/fines de semana parciales).
3. **Horario de mercado** si tampoco está en eToro 24/5 (solo entrar en sesión operable).
