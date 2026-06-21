"""
Sección EARNINGS — generador de candidatos/señales de earnings (sin paper-portfolio).

A diferencia del Piloto (Marea, que simula trades), esta sección NO ejecuta ni simula
posiciones: surface a CANDIDATOS por estrategia con su convicción + un CALENDARIO de próximos
earnings, los mide en el Scoreboard (arm='earnings') y deja que Oscar decida manualmente qué
holdear u operar (acciones / futuros).

Estrategias (hoy): PED (Post-Earnings Drift). Futuras: Short-PED, Cañonazo intradía, pre-run-up.
La math de señal PED se reutiliza de pilot.ped_signals (librería neutral de señal).
"""
