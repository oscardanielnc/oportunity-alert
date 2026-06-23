# 🎯 Scoreboard — Diagnóstico y camino a una solución
# Doc vivo para revisar cada ciertos días. Última actualización: 2026-06-23
# Dueño: Oscar Navarro · Asistente: Claude

> **Decisión de Oscar (2026-06-23):** "lo vamos a ir revisando cada ciertos días, pero ya
> estamos viendo que no lo estamos haciendo bien, ir pensando en una solución, deja todo
> documentado hasta la próxima." → Este doc es el lugar para acumular lecturas del scoreboard
> y desarrollar la solución. NO hay solución decidida aún; abajo van hipótesis a validar.

---

## 📸 Snapshot de la 1ª lectura (2026-06-23, ~14:10 Lima)

Scoreboard empezó a registrar el **2026-06-21** (hace ~2.5 días). Estado tabla `signal_outcomes`:

- **55 señales** registradas: 34 `market_movers` + 21 `noticias`.
- **8 cerradas** (cada señal tarda 48h en resolver) · **47 abiertas**.
- De las 8 cerradas, solo **6 con dirección predicha** (las otras 2 = market_movers sin dirección, mixto/incierto).

### Las 8 cerradas (retorno `abn_final` = anormal vs QQQ al exit)

| Ticker | Brazo | Predijo | abn_final | MFE | dir_correct | Sector |
|--------|-------|---------|-----------|-----|-------------|--------|
| MRVL | market_movers | LONG | **−10.3%** | +0.7% | ❌ | SMH |
| NBIS | market_movers | LONG | **−9.3%** | +3.0% | ❌ | XLK |
| INTC | market_movers | — | −5.3% | +3.8% | (sin dir) | SMH |
| AMD | market_movers | LONG | −3.9% | +4.0% | ❌ | SMH |
| MU | market_movers | LONG | −3.4% | **+8.7%** | ❌ | SMH |
| TSLA | noticias (contract_govt) | LONG | −2.2% | +2.9% | ❌ | XLY |
| TSLA | market_movers | — | −0.6% | +2.9% | (sin dir) | XLY |
| COHR | market_movers | LONG | +0.9% | **+10.8%** | ✅ | SMH |

**Acierto direccional: 1/6 = 17%.** Todas las señales con dirección eran **LONG**.

---

## 🔍 Diagnóstico (qué está fallando y por qué)

### 1. Las señales ignoran el régimen macro/sectorial → fallo de contagio
5 de 6 señales LONG eran **semiconductores** (MRVL, NBIS, AMD, MU, COHR) llamadas alcistas
**justo mientras SMH se daba vuelta**. Como `abn_final` es anormal vs QQQ, un −10% en MRVL = cayó
10% **más que el mercado** → los semis se llevaron la peor parte y el sistema seguía diciendo LONG.
**Es el caso de libro que ataca el Brief nuevo (radar de sectores + contagio), pero el pipeline de
señales/alertas todavía NO tiene un gate de régimen/sector que frene los LONG en sector cayendo.**

### 2. Desajuste de horizonte/salida → el edge es temprano y se diluye a 48h
Los MFE grandes lo delatan: **MU +8.7%** y **COHR +10.8%** tuvieron un *pop* fuerte y tradeable
**antes** de revertir al cierre de 48h. La reacción inicial muchas veces iba bien; lo que falló fue
**sostenerla hasta el exit de 48h**. Esto es problema de **horizonte/timing de salida**, no
necesariamente de dirección. Concuerda con [[news-reaction-findings]]: llegamos tarde, el edge vive
en lo intradía y se evapora.

### 3. Consistente con hallazgos previos
- El score de la IA **no calibra** (señales fuertes no rinden más que débiles).
- Problema de **cobertura + velocidad**: llegamos tarde a los movimientos grandes.

### ⚠️ Caveat honesto
**6 señales, 2.5 días, todo en un washout de semis/tech.** No es concluyente estadísticamente.
La lectura cualitativa SÍ es clara y útil, pero no juzgamos al sistema hasta tener muestra real.

---

## 💡 Hipótesis de solución (a desarrollar — NINGUNA decidida)

> Pensar/priorizar en próximas revisiones. Orden ≈ relación impacto/esfuerzo.

1. **Gate de régimen sectorial sobre las señales (código puro, barato).**
   No abrir/avisar LONG en un ticker cuyo ETF de sector está *rolling over* (ret 5d − / bajo EMA20 —
   ya lo calcula `_sector_radar_block`). Opciones: suprimir la señal, bajarle prioridad, o invertir a
   "esperar/SHORT". Reusa el radar que ya desplegamos. **Candidato #1.**

2. **Rediseño de horizonte/salida para capturar el MFE.**
   El edge está temprano. Explorar: medir/actuar en ventana intradía (no 48h), o salida con trailing
   que capture el pop (+X% o tras N horas). El scoreboard ya guarda `time_to_peak_min` → **analizar
   cuándo ocurre el pico** para fijar la ventana óptima.

3. **Segmentar la medición por régimen.**
   Taggear cada señal en t0 con: `risk_on` (régimen macro), `sector_rolling_over`, `breadth`. Así el
   scoreboard aprende "la señal funciona en régimen X, falla en Y" en vez de promediar todo. Hoy NO
   se guardan esos tags → es trabajo de instrumentación en `scoreboard.record_signal`.

4. **Revisar la fuente de dirección de `market_movers`.**
   La dirección sale de `impacto` (alcista→LONG / bajista→SHORT) de la declaración macro. Validar si
   esa etiqueta es demasiado cruda (¿genera LONG espurios?).

5. **Exigir momentum anormal positivo en la entrada.**
   Igual que `vsSPY` beta-ajustado del dip scanner: no entrar LONG si el retorno anormal reciente ya
   es negativo (el ticker viene perdiendo vs su beta).

---

## 🧪 Para que la PRÓXIMA revisión sea concluyente (instrumentación pendiente)

- [ ] Taggear señales en t0 con régimen (`risk_on`), `sector_rolling_over`, `breadth` (hipótesis #3).
- [ ] Analizar `time_to_peak_min` de las cerradas → ¿el pico es en las primeras horas? (hipótesis #2).
- [ ] Acumular **≥30 señales cerradas** y, ojalá, en **régimen mixto** (no solo este washout).
- [ ] Segmentar win-rate por brazo × régimen × sector-girando.

---

## 🔁 Bitácora de revisiones

- **2026-06-23** — 1ª lectura (8 cerradas, 1/6 dir). Diagnóstico inicial: señales LONG ignoran
  contagio sectorial + horizonte de 48h diluye el edge temprano. Sin acción de fix aún (muestra
  mínima); solo documentado. Brief estratégico (radar+contagio) ya desplegado hoy — primera defensa
  parcial contra el fallo #1.

---

*Relacionado: `NEWS_REACTION_PLAN.md`, `RESTRUCTURE_3ARMS.md`, memorias [[news-reaction-findings]],
[[brief-diario-estrategico]], [[scoreboard-diagnostico]].*
