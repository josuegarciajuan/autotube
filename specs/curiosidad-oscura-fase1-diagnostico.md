# Fase 1 — Diagnóstico de packaging: matriz de fallos reproducidos

> Fecha: **2026-10-23**. Suite de reproducción: `tests/test_packaging_repro_fase1.py`
> (**9 tests**). Estado: **los 6 fallos reproducidos quedaron corregidos en la
> Fase 2** — los 7 tests que eran `xfail(strict=True)` pasan en verde; las
> suites relacionadas suman **168 passed**.
> Suites existentes verificadas: **77 passed** en la Fase 1 (antes del fix).
>
> Fallo preexistente ajeno a esta fase: `test_preflight_cleanup_preserves_recent_files`
> (stub `_FakeDB` sin `get_oldest_active_job_start_epoch`; también falla en producción).

Cada fallo está reproducido con las **funciones reales** del pipeline y mapeado
a su etapa y causa raíz. «Esperado» = comportamiento que la Fase 2 implementará.

## Etapas del pipeline de packaging

```
[T1] TitleEngine selecciona título completo (guardia is_complete_title)
[T2] metadata_generator:646 truncate_title_at_word(title, 65)   ← post-selección
[T3] metadata_generator:647 validate_title → SOLO observabilidad (no bloquea)
[O1] LLM (brainstorm/marketing) propone L1/L2 crudos
[O2] packaging_brief.build_overlay_spec: dedup de tokens + sustitución saliente
[O3] metadata_generator persiste thumbnail_text (DB) y valida (gate de subida)
[P1] thumbnail_maker._compose_final: red de seguridad [:budget]
[P2] thumbnail_maker._compose_final: pintado con fuente fija (% del alto)
```

## FALLO 1 — L2 mutilada por dedup de tokens

| | |
|---|---|
| **Evidencia prod.** | 2733: `10 CASOS \| LA CALLÓ`; 2649: `BAJO LA SELVA \| LOS` |
| **Etapa** | O2 (`build_overlay_spec`, `packaging_brief.py:193-196`) |
| **Causa raíz** | `banned_for_l2 = title_terms ∪ tokens(L1)` y se eliminan de L2 **todas** las palabras baneadas. Si L2 era «LA CIENCIA LO CALLÓ» y el título contiene «ciencia», queda «LA LO CALLÓ» (o «LA CALLÓ»/«LOS» según el caso). La regla anti-repetición destruye la gramática. |
| **Repro** | `test_l2_no_se_mutila_por_compartir_palabra_con_titulo` + `test_l2_nunca_queda_como_resto_gramatical` |
| **Esperado** | Compartir una palabra con el título es lícito. Si L2 repite **demasiado**, se reformula la frase completa (o se vacía); jamás se poda token a token dejando un resto. |

## FALLO 2 — Término saliente sin significado

| | |
|---|---|
| **Evidencia prod.** | 2697: `QUIÉN \| BAJO LA ARENA`; 2735: `HMS \| EL HIELO LOS BORRÓ` |
| **Etapa** | O2 (`title_terms` + `_salient_title_term`, `packaging_brief.py:107-130`) |
| **Causa raíz** | (a) `_STOPWORDS` no incluye interrogativos/adverbios («quién», «cómo», «dónde», «cuánto»…), así que «quien» entra como término de contenido. (b) `_salient_title_term` prioriza dígitos > primer nombre propio > más largo: en «Hms Erebus y Terror…» elige «Hms» por ser el primer capitalizado, aunque es un prefijo de navío sin carga. Además la sustitución de L1 se dispara cuando L1 no comparte token con el título, y el sustituto hereda ese saliente débil. |
| **Repro** | `test_interrogativos_no_son_terminos_de_contenido` + `test_saliente_prefiere_contenido_sobre_token_debil` |
| **Esperado** | Interrogativos y adverbios nunca son términos de contenido. El saliente prefiere el token con más carga (nombre propio con ≥4 letras o dígito); prefijos tipo «HMS/USS/RMS» no son elegibles. |

## FALLO 3 — Pintado sin ajuste al ancho real

| | |
|---|---|
| **Evidencia** | Medición con fuentes reales (`_find_font`, 1280×720): ruta de **1 línea** usa fuente `max(80, 0.19·720)=136px` → «GUERRAS MUNDIAL» (14 chars, dentro de presupuesto) mide **1403px > 1280px**: desborda. Ruta de 2 líneas a 115px queda a ≤17px del borde (sin margen seguro). `_fit_text_to_box` existe pero solo se usa para badges/insets, no para L1/L2 (`thumbnail_maker.py:1570-1616`). |
| **Etapa** | P2 |
| **Repro** | `test_l1_dentro_de_presupuesto_cabe_con_margen_en_layout_1_linea` |
| **Esperado** | L1/L2 se dibujan con ajuste al ancho disponible (márgenes seguros), reduciendo fuente o re-maquetando antes de pintar. |

## FALLO 4 — Recorte duro `[:budget]` a mitad de palabra

| | |
|---|---|
| **Evidencia** | `thumbnail_maker.py:1565-1566`: `text_gancho_v[:budgets["l1"]]` — «GUERRAS MUNDIAL» → «GUERRAS MUNDIA». Solo se dispara si el texto llega sin pasar por `build_overlay_spec` (ruta legacy `overlay_text`), pero existe y corta palabras. |
| **Etapa** | P1 |
| **Repro** | `test_red_de_seguridad_no_corta_a_mitad_de_palabra` |
| **Esperado** | La red de seguridad recorta en frontera de palabra (como `_truncate_words`) o no recorta. |

## FALLO 5 — Título completo truncado tras la selección

| | |
|---|---|
| **Evidencia prod.** | 2733 «…los 10 casos que la ciencia»; 2594 «…enigmas que aún»; 2480 «…que la NASA aún no logra» (todos cortados a ~60-65 chars a mitad de cláusula) |
| **Etapa** | T2 (+T3 como no-bloqueo) |
| **Causa raíz** | TitleEngine ya garantiza completitud en la **selección** (`is_complete_title`), pero `metadata_generator.py:646` aplica `truncate_title_at_word(title, 65)` **después**, re-cortando a mitad de cláusula. `validate_title` (:647) detecta `incomplete_phrase` pero el resultado solo se registra en observabilidad (:717) — **no bloquea ni regenera**. La ruta legacy de fallback (`apply_caps_policy(truncate=True)`) trunca igual. Nota: `title_is_complete` de `packaging_brief` (F5) **no** detecta verbos de apertura ni adverbios colgantes — la guardia buena es `title_tokens.is_complete_title`. |
| **Repro** | `test_paso_post_seleccion_nunca_rompe_titulo_completo` |
| **Esperado** | Ningún paso post-selección rompe la completitud: si no cabe, se rechaza y se regenera/reelige entre candidatos completos; nunca se publica un corte a medias. |

## FALLO 6 — Incoherencia imagen ↔ tema (observado, sin test unitario)

| | |
|---|---|
| **Evidencia prod.** | 2733 (viajeros del tiempo): miniatura = camiseta «oatmilk» sobre fondo negro. |
| **Etapa** | F3 del maker (`_generate_subject_image` / art director) — proveedor de imagen. |
| **Hipótesis** | El `primary_subject` derivado del brief no ancla al tema (igual que el FALLO 2 en texto) y el fetch de stock/IA resuelve con un concepto genérico; además `text_overlay`/`subject_noun` no se verifican contra la imagen elegida. |
| **Plan** | Investigar en Fase 2 con trazas de `ThumbnailBrief.primary_subject` + `art_director.plan` sobre el vídeo 2733; la validación de coherencia visual entra como revisión (Fase 2.4). |

## Resumen: qué corrige la Fase 2 (mapeo directo)

| # | Fix | Archivo(s) |
|---|---|---|
| 1 | Eliminar la poda token-a-token de L2; reformulación completa si repite | `pipeline/packaging_brief.py` |
| 2 | Interrogativos/adverbios fuera de `title_terms`; saliente con filtro de calidad (≥4 letras, sin prefijos navales); sustitución de L1 usa `subject_noun` del brief antes que el saliente | `pipeline/packaging_brief.py` |
| 3 | Ajuste de fuente al ancho con márgenes en las rutas de 1 y 2 líneas (usar `_fit_text_to_box` o equivalente) | `pipeline/thumbnail_maker.py` |
| 4 | Red de seguridad `[:budget]` → recorte en frontera de palabra | `pipeline/thumbnail_maker.py` |
| 5 | Quitar el truncado post-selección; si no cabe, reelegir candidato completo; `validate_title` bloquea en generación (no solo log) | `pipeline/metadata_generator.py` |
| 6 | Anclaje de `primary_subject` al tema + verificación de coherencia imagen-tema | `pipeline/thumbnail_brainstorm.py` / `thumbnail_art_director.py` |
