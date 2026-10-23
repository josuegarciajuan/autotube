# Fase 3 — Modelo editorial «curiosidad oscura» configurable por canal

> Fecha: **2026-10-23**. Implementación: `pipeline/editorial_policy.py` +
> parámetros en `config/defaults.py` + overrides en `config/canal{2,3,4,5}_config.py`.
> Tests: `tests/test_editorial_policy.py` (**16 passed**).

## Principio

El giro editorial es **configuración, no código**. Ningún servicio ramifica por
slug ni por `channel_id`: todo se resuelve con
`config_bridge.get_channel_config(slug)`. La política resuelta se construye una
vez con `build_policy(cfg)`.

## Contrato del modelo

### Entradas (config, con defaults universales)

| Clave | Default | Significado |
|---|---|---|
| `EDITORIAL_CURIOSITY_ENABLED` | `True` | Kill-switch por canal |
| `EDITORIAL_CURIOSITY_PILOT_SHARE` | `0.5` | Fracción de (canal, día) en piloto |
| `EDITORIAL_CURIOSITY_WEIGHT` | `0.25` | Peso de la señal en el ranking (Fase 4) |
| `EDITORIAL_TENSION_TYPES` | 6 tipos | Tipos válidos para el canal |
| `EDITORIAL_TENSION_WEIGHTS` | ver tabla | Peso por tipo (`0.0` = vetado) |
| `EDITORIAL_TENSION_LEXICON` | raíces ES | Clasificador determinista (0 coste) |
| `EDITORIAL_REQUIRE_FACTUAL_SUPPORT` | `True` | Sin evidencia no entra al piloto |
| `EDITORIAL_EXCLUDED_CATEGORIES` | 5 categorías | Temas que nunca reciben el giro |
| `EDITORIAL_RECENT_DIVERSITY_MIN` | `2` | Diversidad mínima entre temas recientes |
| `EDITORIAL_TENSION_SEEDS` | `[]` | Semillas de búsqueda del giro (Fase 4) |

Kill-switch global en runtime: `system_state["editorial_curiosity_disabled"]`.

### Salidas (API del módulo)

| Función | Uso |
|---|---|
| `build_policy(cfg)` | Política resuelta (fail-open a defaults) |
| `classify_tension(text, cfg)` | `{tipo: score}` — solo tipos presentes |
| `dominant_tension(text, cfg)` | Tipo dominante o `""` |
| `editorial_score(text, cfg)` | Señal normalizada `[0,1]` para el ranking |
| `is_sensitive(text, cfg)` | Categoría excluida detectada |
| `is_eligible(text, cfg, has_factual_support=…)` | Gate del piloto |
| `pilot_decision(slug, day, cfg)` | `pilot` \| `control` \| `disabled` (determinista) |
| `editorial_label(text, cfg)` | Etiqueta persistible `editorial:<tipo>` |

### Reglas duras

1. **El modelo no sustituye gates.** Nicho, demanda, respaldo factual y
   `content_safety` siguen siendo requisitos independientes.
2. **Piloto determinista.** `pilot_decision` deriva de
   `sha1(canal|día|editorial-pilot)`: un reintento del mismo día no cambia de
   grupo (medición estable en Fase 6). No usa `random`.
3. **Fail-open.** Config ausente/corrupta → defaults; sin red ni excepciones.
4. **Sin hardcoding.** Prohibido `if slug == ...` / `channel_id == ...`
   (test de invariante incluido).
5. **Seguridad primero.** Los temas sensibles (menores, autolesión, suicidio,
   violencia gráfica, desinformación sanitaria) quedan **excluidos del giro**
   aunque tengan tensión. `content_safety` sigue aplicando su propio filtro.

## Preferencias por canal

| Canal | Giro editorial | Pesos de tensión (1.0 = máximo) |
|---|---|---|
| **canal2** Sincronías | Coincidencias inquietantes, premoniciones, decisiones que cambian destinos, desapariciones inexplicables | anomalía 1.0 · misterio 0.9 · peligro 0.7 · supervivencia 0.7 · muerte 0.6 · colapso 0.4 |
| **canal3** Civilizaciones Olvidadas | Colapsos, ciudades abandonadas de golpe, asedios, coste humano de los hallazgos | colapso 1.0 · muerte 0.8 · anomalía 0.8 · peligro 0.7 · misterio 0.7 · supervivencia 0.6 |
| **canal4** Expediciones sin retorno | Puntos de no retorno, aislamiento, supervivencia extrema | peligro 1.0 · supervivencia 1.0 · muerte 0.9 · colapso 0.6 · misterio 0.6 · anomalía 0.5 |
| **canal5** Anomalías Médicas | Casos clínicos desconcertantes y límites del cuerpo (sin prometer curas) | anomalía 1.0 · supervivencia 0.9 · peligro 0.6 · muerte 0.6 · misterio 0.6 · colapso 0.3 |

Todos arrancan con `PILOT_SHARE = 0.5` (mitad piloto / mitad control) y
`WEIGHT = 0.25`, revisables en la Fase 7.

### Semillas del giro (por canal)

- canal2: coincidencias imposibles · premoniciones que se cumplieron ·
  decisiones que cambiaron el destino · desapariciones inexplicables ·
  casos de sincronicidad reales.
- canal3: ciudades abandonadas de golpe · civilizaciones que desaparecieron ·
  el último día de la ciudad · sacrificios documentados · colapso de
  civilizaciones antiguas.
- canal4: expediciones que terminaron en tragedia · nadie sobrevivió a la
  expedición · supervivientes de expediciones extremas · la última expedición
  sin retorno · atrapados sin salida en la montaña.
- canal5: casos médicos inexplicables · enfermedades raras documentadas ·
  pacientes que sobrevivieron a lo imposible · síntomas que la medicina no
  explica · el cuerpo humano al límite.

Las semillas **se suman** a las SEO existentes (`seeds_from_config`), no las
sustituyen. La Fase 4 las integra en el seeding.

## Comportamiento de reserva

Si no hay candidatos elegibles (sin tensión, sensibles o sin respaldo), la
Fase 4 **conserva el orden actual** y el canal publica con su orientación
habitual. El modelo nunca fuerza un tema fuera de nicho ni bloquea generación.

## Caso de referencia (validación)

> «La posada donde nadie salió con vida: el hielo los atrapó sin salida»
> — canal4 → `{peligro: 1.0, muerte: 0.9}`, dominante `peligro`,
> score 1.0, elegible. Es el vídeo más visto del sistema (1.251 visitas).

## Fuera de alcance (siguiente fase)

Integrar `editorial_score` y `EDITORIAL_TENSION_SEEDS` en el ranking de temas
(`orchestrator.phase_generate_script` / `topic_seeding`), aplicar
`EDITORIAL_RECENT_DIVERSITY_MIN` y persistir `editorial_label` + grupo
piloto/control por vídeo.
