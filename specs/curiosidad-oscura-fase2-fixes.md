# Fase 2 — Corrección del packaging (curiosidad oscura)

> Fecha: **2026-10-23**. Cierra los 6 fallos de la Fase 1. Validación:
> `tests/test_packaging_repro_fase1.py` (9/9) + **168 tests** de las suites de
> packaging/título en verde.

## Cambios

### 1. L2 sin poda token a token — `pipeline/packaging_brief.py` · `build_overlay_spec`
Antes: se borraban de L2 todas las palabras presentes en el título o en L1
(dejaba restos como «LA CALLÓ», «LOS»).
Ahora: la decisión es **sobre la frase completa**. L2 solo se vacía cuando es
una casi-copia (solape de tokens de contenido > 60 %, o repetida literal del
título/L1). Compartir alguna palabra con el título es lícito y la frase se
conserva íntegra.

### 2. Saliente con significado — `_STOPWORDS`, `_salient_title_term`
- Interrogativos/adverbios («quién», «cómo», «dónde», «cuánto»…) fuera de
  `title_terms`: nunca más L1 = «QUIÉN».
- `_WEAK_SALIENT`: prefijos navales (HMS/USS/RMS…), tratamientos y genéricos
  («caso», «historia») no son elegibles.
- Todo candidato necesita ≥ 4 letras salvo que contenga un dígito. Si no hay
  ancla válida se devuelve "" y el llamador **conserva el L1 original** (mejor
  el gancho del LLM que basura).
- Nuevo `salient_subject(title)` público (ancla de sujeto reutilizable).

### 3. Sustitución de L1 en cascada
Si L1 no ancla al título: (1) palabras del `subject_noun` presentes en el
título; (2) saliente de calidad; (3) se conserva el original.

### 4. Recorte en frontera de palabra — `pipeline/thumbnail_maker.py`
`text[:budget]` → `packaging_brief._truncate_words(...)`: nunca corta una
palabra a medias («GUERRAS MUNDIAL» → «GUERRAS», no «GUERRAS MUNDIA»).

### 5. Ajuste de fuente al ancho real — `thumbnail_maker._fit_font_size`
L1 y L2 se pintan ahora con el mayor tamaño que **cabe en el lienzo con
margen seguro** (4 % por lado, mínimo 40 px). Antes el tamaño era fijo por
altura y «GUERRAS MUNDIAL» a 136 px medía 1403 px sobre 1280 px.
Smoke test de composición real: miniatura generada, texto dentro de márgenes.

### 6. Título completo o re-elección — `pipeline/metadata_generator.py`
- Nuevo `_pick_complete_title(candidates, max_chars)`: primer candidato
  **completo y dentro de presupuesto**.
- Nuevo `_ensure_complete_title(title, script)`: guardia final; ninguna ruta
  emite un corte a mitad de cláusula.
- Ruta principal: se elimina el `truncate_title_at_word` post-selección; si el
  título no cabe o está incompleto se reelige (candidatos del motor → título
  del LLM → fallbacks) y, en último recurso, `finalize_title` validado.
- Ruta `_fallback_metadata`: misma política.
- `generate_alternative_title`: si el corte deja el título incompleto, se
  conserva el título original (una alternativa rota es peor que ninguna).

### 7. Sujeto visual anclado al tema — `pipeline/thumbnail_brainstorm.py` · `_merge`
Si el agente no define `primary_subject`, se deriva del **término saliente del
título** (`salient_subject`) y no de un concepto genérico. Ataca la causa del
caso 2733 (miniatura con una camiseta ajena al tema).

## Verificación

```
pytest tests/test_packaging_repro_fase1.py            → 9 passed
pytest <15 suites de packaging/título/metadata>       → 168 passed
```
Smoke test de composición (sin LLM): `_compose_final` genera la miniatura con
L1 ajustado y dentro de márgenes.

Pendiente fuera de alcance: `test_preflight_cleanup_preserves_recent_files`
falla también en producción (stub `_FakeDB`), no es regresión de esta fase.
