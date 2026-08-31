# ADR-0002: UTC para instantes y Europe/Madrid para planificación

**Estado:** Accepted
**Fecha:** 2026-08-31

## Contexto

SQLite almacena varias columnas de tiempo sin zona y el panel podía interpretar
el mismo valor en la zona local del navegador. Esto desplazaba publicaciones y
hacía inconsistentes los topes diarios.

## Decisión

Los instantes persistidos y comparados se normalizan a UTC; SQLite conserva el
formato UTC-naive por compatibilidad. Las fechas de planificación y los topes
diarios usan Europe/Madrid. YouTube recibe RFC3339 UTC y el frontend fuerza
Europe/Madrid al mostrar fechas.

La migración histórica se ejecuta explícitamente, primero con:

```bash
python3 scripts/migrate_timestamps.py --dry-run
python3 scripts/migrate_timestamps.py
```

## Consecuencias

- Se evitan comparaciones naive local/UTC y el resultado no depende del host ni
  del navegador.
- Los registros históricos ambiguos de planificación se interpretan como hora
  Madrid; campos generados por `CURRENT_TIMESTAMP` y publicación real no se
  reinterpretan.
- El cambio requiere revisar el informe del dry-run antes de aplicarlo.
