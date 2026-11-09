# Registro de experimentos de crecimiento

Cada experimento se registra **antes** de empezar. Sin ficha previa no se
considera experimento: es un cambio sin medición.

## Plantilla

| Campo | Contenido |
|---|---|
| ID | E1…E5 |
| Hipótesis | Qué creemos y por qué |
| Evidencia | Cifras y ventana que la sustentan |
| Cambio | Qué se modifica exactamente |
| Métrica principal | Una sola, con fórmula |
| Guardarraíl | Qué no puede empeorar |
| Muestra mínima | Vídeos y periodo |
| Criterio de éxito | Umbral pre-registrado |
| Criterio de reversión | Cuándo se revierte |
| Inicio / fin | Fechas |
| Estado | pendiente / en curso / concluido |

## Reglas

1. Una métrica principal por experimento.
2. No cambiar dos variables a la vez (título **o** miniatura; tema **o** CTA).
3. Comparar cohortes de edades equivalentes o ventanas completas.
4. El CTR de shorts y largos no se compara entre sí.
5. Si el A/B no alcanza la muestra, se usa cohorte pre/post y se declara.
6. Todo resultado se anota aquí y en `docs/auditoria-mejoras-2026-11-09.md`.

## Herramientas

```bash
# Cohorte pre/post de un canal y formato
python3 scripts/growth_experiment_report.py --slug canal5 --format long \
  --a 2026-10-10:2026-11-06 --b 2026-11-07:2026-12-04

# Sembrar temas de largos desde shorts ganadores (E3)
python3 scripts/seed_longform_topics_from_shorts.py --slug canal2 --limit 10 --dry-run
```
