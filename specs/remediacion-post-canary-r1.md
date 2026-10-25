# Remediación post-canary R1 (sin reabrir P00–P05)

Estado: **remediación acotada**, no P06. P04/P05 siguen apagados por defecto.

El canary expandido (script 1715, 127 rangos fijados) expuso cuatro defectos
concretos y un fallo de robustez de la biblia visual. Esta spec fija el
comportamiento esperado de la corrección. No activa producción.

## 1. Coherencia en el rerank integrado

`IntegratedSelector.rank_universe` era el camino real de P05, pero leía
`meta["coherence"]` sin que nadie lo rellenase. `_rank_pool` sí llamaba a
`SelectionCoherence.evaluate`, de ahí que la coherencia funcionase en P04 y no
en el canary integrado.

Regla:

- Todo candidato del universo integrado recibe señales de la **misma**
  instancia `SelectionCoherence` que después registra el ganador real.
- La penalización se aplica solo a candidatos dentro de
  `best_relevance - coherence_band` (`0.08`).
- El score unificado la limita a `coherence_cap` (`0.05`).
- `bounded_coherence` no vuelve a penalizar proveedor/still en filas que ya
  llevan una penalización de contexto (`coherence_applied_penalty > 0`).
- Duplicados exactos siguen siendo veto duro (`dedup`). Proveedor, shot,
  still y estilo son señales blandas; era solo penaliza cuando hay evidencia.

Invariante: **RELEVANCE > COHERENCE**. Una diferencia de relevancia mayor que
el cap no puede invertirse por diversidad.

## 2. Suelo de calidad post-failover

Si el candidato aprobado de arriba falla al descargar, el siguiente no puede
ganar a cualquier precio. El suelo usa la calibración existente:

- `min_relevance_score` = `0.18` (suelo P03).
- `strong_relevance_score - min_relevance_score` = `0.12` (caída tolerable
  desde un candidato fuerte a uno adecuado).
- `post_failover_fallback_floor` = `conceptual_min_score` = `0.10` para
  conceptual/generic.

`floor = max(0.18, top_final - 0.12)` para activos reales. Si ningún
candidato lo alcanza, el resultado es `NO_GOOD_CANDIDATE` / fallback explícito;
nunca se fabrica un éxito. El guardia solo se activa cuando hubo failover
(descarga rota, cache o duplicado).

## 3. Cache de assets fallidos

Cache **por run** con TTL (`1800 s`) y cota (`512` entradas), nunca blacklist
global. Clave: identidad canónica del candidato + alias de URL. Registra
proveedor, id, URL, categoría, primer fallo y `skip_count`. Escenas
posteriores del mismo run saltan el asset roto sin consumir un intento de
descarga.

## 4. Tipo de contenido Pixabay

El contrato es “imagen raster decodificable”, no “la extensión de la URL debe
coincidir con los magic bytes”. Se aceptan JPEG, PNG y WebP solo tras decodificar
con PIL; PNG/WebP se normalizan a JPEG. HTML, bytes arbitrarios o imágenes
truncadas siguen rechazándose. El tamaño mínimo de 50 KB se mantiene.

## 5. Duración del plan de movimiento

La selección P04 crea el plan con la duración de la escena previa al split de
timing de imágenes. `_reconcile_actual_image_fallbacks` reescribe
`motion_plan.duration` con la duración final de cada escena/subescena. El
perfil de zoom/pan no cambia (sin zoom excesivo).

## 6. Robustez de la biblia visual

- `parse_json_lenient` recupera fences markdown, prosa alrededor del JSON,
  comas finales y JSON truncado por límite de tokens (sin `eval`).
- La llamada principal usa `retries=1`, `max_tokens` y timeout acotados;
  el failover de modelos sigue existiendo.
- El gap-fill usa lotes pequeños (12 por defecto), divide lotes fallidos y
  reintenta; conserva entradas válidas parciales y continúa con los lotes
  posteriores.
- Un fallo de un lote no puede convertir las escenas restantes en padding.
- `last_stats` expone cobertura primaria, recuperada, padding, lotes fallidos,
  uso de modelos, latencia y tokens aproximados.

## 7. Fuera de alcance

- No se toca licenciamiento (UNKNOWN sigue sin autorización de publicación).
- No se activan P04/P05 por defecto.
- No hay subida ni publicación.
