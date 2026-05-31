# 🔄 Revalidación mensual — Marea + PED + Estrellas

**Por qué:** Marea/PED NO se calibran por ticker (reglas fijas). Pero los sectores dominantes
y la fuerza relativa ROTAN. Una vez al mes re-corremos las validaciones para confirmar que
el edge sigue vivo y refrescar el modelo de estrellas/sectores. El universo (top-80 liquidez)
ya se refresca solo cada semana — esto NO.

**Última validación:** 2026-05-30
**Próxima:** ~2026-06-30 (mensual)

## Qué correr (3 comandos)
```bash
# 1. Marea out-of-sample (¿sigue batiendo a QQQ por año? ¿diversificada?)
python research/backtest_marea_broad.py

# 2. PED (¿mega-cap + reacción ≥+5% + D+7 sigue con edge?)
python research/backtest_ped.py --fresh

# 3. Score de estrellas (¿el TOP sigue prediciendo retorno forward?)
python research/backtest_stars.py
```

## O más simple
Decile a Claude: **"corré la revalidación mensual de Marea/PED/estrellas"** y los corre + interpreta.

## Qué mirar en los resultados
- **Marea:** ¿bate a QQQ en la mayoría de años? ¿concentración sana (no 1 ticker)?
- **PED:** ¿mega-cap+≥5%+D7 sigue net-positivo out-of-sample?
- **Estrellas:** ¿3⭐ (top tercil) > resto en retorno forward? Si dejó de separar → revisar.
- Si algo dejó de funcionar → NO operar a ciegas; replantear con datos.

> El universo se refresca solo (`pilot/universe.py`, semanal). Esto es solo la revalidación de edge.
