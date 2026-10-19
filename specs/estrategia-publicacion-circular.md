# Estrategia de publicación circular (4 canales × 4 fases)

## Objetivo

Aumentar de forma suave el rango de publicación sin ráfagas ni canales
sincronizados. Cada canal publica su programación base (la del panel) más un
bonus de fase, y los cuatro canales mantienen siempre fases distintas.

## Fases

| Fase | Bonus |
|---|---|
| `normal` | ninguno |
| `shorts+1` | +1 short nativo |
| `largos+1` | +1 long-form |
| `random+1` | +1 long-form **o** +1 short nativo |

En `random+1` el tipo se decide una vez por día de forma determinista y
balanceada: cada bloque de 4 días contiene exactamente 2 extras `long` y 2
`short`. Reinicios y replanes no cambian la decisión.

## Rotación

1. En la primera ejecución se sortea una permutación inicial (sin fases
   repetidas) y se persiste con fecha ancla (`anchor_date`) en
   `system_state["rotation_strategy_v1"]`.
2. Cada día natural de Madrid cada canal avanza una posición en el ciclo
   `normal → shorts+1 → largos+1 → random+1 → normal`.
3. Como la asignación inicial es una permutación de 4 fases, cada día los 4
   canales tienen fases distintas. En 4 días cada canal pasa exactamente una
   vez por cada fase.
4. La asignación del día se materializa en
   `system_state["rotation_day_<YYYY-MM-DD>"]` (idempotente).

Fecha/hora de referencia: `Europe/Madrid` (`api.time_utils.MADRID`). El ciclo
avanza todos los días naturales (incluidos sábados y domingos).

## Autoridad y precedencia

La rotación es la **autoridad de planificación diaria**:

```
base (Configuración de Programación / perfil de entrega / estado del canal)
  + bonus de la fase del día
  = objetivo efectivo de publicación
```

- `resolve_channel_policy_values(..., day=...)` aplica el bonus por fecha.
- Ningún loop automático puede recortar el bonus: planificación de horizonte,
  `smart_replan`, `sync_midday`, top-up, recovery planner, cobertura de
  publicación, repack, válvula de shorts, reconciliación de caps y “Reprogramar
  Ahora” consultan el mismo resolvedor.
- Los bonos long y short se aplican por separado. `upload_capacity_per_day` se
  deriva del cap long efectivo (incluye +1 long).
- La rampa post-strike (`gradual_resume`) escribe SIEMPRE la cadencia base:
  usa `include_rotation=False` para no convertir el bonus en config permanente.
- El boost aleatorio legacy (`videos_day_boost_weight`) y `alternate_pattern` se
  ignoran mientras la rotación está activa (evita doble extra).

## Seguridad (invariantes que NO se relajan)

La rotación nunca salta:

- strike confirmado y bloqueo de subidas (`shorts_spam_blocked_until_*`);
- cuota agotada del proyecto GCP;
- espaciados mismo canal y espaciado global entre canales;
- ventana máxima de calentamiento;
- límite de subidas por cuenta Google;
- verificación post-subida y filtros de contenido.

Si un bloqueo impide publicar, el objetivo se conserva y el replan lo difiere.
No se usan rutas `force_immediate` ni bypasses manuales.

## Maratones

Mientras la rotación está activa, `check_and_dispatch_marathon()` no despacha
maratones: publican muchos long-forms fuera del plan diario. Kill-switch
explícito: `rotation_strategy.allow_marathons=true` o desactivar la rotación.

## Reprogramación diaria

- Loop supervisado `rotation_daily` (`api/main.py`, cada 5 min, 0 cuota).
- Al cambiar el día de Madrid: materializa la nueva asignación y ejecuta un
  replan coherente de long-form (horizonte 7 días, `force_replan=True`, sin tocar
  jobs `running`) + shorts.
- En arranque, la fase del día se materializa antes del planning engine.
- Idempotente: varias pasadas el mismo día no duplican trabajo.
- Respeta `scheduler_paused` y la ventana silenciosa de un replan manual; si la
  ventana está activa, el plan manual ya se construyó con los caps por fecha.

## API

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/pacing/rotation` | Estado, fases de hoy y previsualización de 4 días |
| PUT | `/api/pacing/rotation` | `{enabled, reset, allow_marathons}`; aplica replan |

Ejemplos:

```bash
# Ver estrategia
curl -s localhost:8000/api/pacing/rotation | jq

# Desactivar (kill-switch operativo)
curl -X PUT localhost:8000/api/pacing/rotation \
  -H 'Content-Type: application/json' -d '{"enabled":false}'

# Reactivar y resurtir una permutación nueva desde hoy
curl -X PUT localhost:8000/api/pacing/rotation \
  -H 'Content-Type: application/json' -d '{"reset":true,"enabled":true}'
```

Kill-switch por entorno: `ROTATION_STRATEGY_DISABLED=1` (máxima prioridad,
el resolvedor devuelve la programación base).

## Persistencia

No requiere migración de esquema: usa `system_state` (ya persistente y
respaldado con la DB).

- `rotation_strategy_v1`: estrategia, ancla, participantes, permutación inicial,
  seed, flag `enabled`, `allow_marathons`.
- `rotation_day_<fecha>`: asignación materializada del día.

## Pruebas

`tests/test_rotation_strategy.py` cubre:

- permutación inicial sin repetición;
- 4 días con fases distintas y ciclo completo por canal;
- `random+1` estable y 2 long / 2 short por bloque;
- persistencia idempotente del día;
- +1 long / +1 short exactos y `upload_capacity_per_day`;
- fase `normal` y rotación desactivada sin cambios;
- no participantes sin bonus;
- planificación por fecha y sin doble boost legacy;
- cache de cap por día;
- gaps anti-spam intactos y strike sin resurrección.

Suites relacionadas: política de canal, pacing, planificación, allocator,
repack, cobertura, cuota, post-strike, maratones, replan seguro y shorts.
