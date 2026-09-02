# Diseño: rango adaptable de duración para escenas de imagen

**Estado:** Aprobado para implementación posterior
**Fecha:** 2026-08-25
**Propietario:** Pipeline de generación de vídeo
**Próxima revisión:** después de la primera ejecución en producción o, como máximo, en 30 días

## Resumen

Ampliar el rango permitido para escenas cuyo recurso final es una imagen a **4–15 s**.
El cambio afecta al contrato específico de imágenes, al validador de configuración y al
reconciliador de fallback de `MediaFetcher`. No cambia los límites legacy de escena ni los
límites específicos de vídeo.

Este documento describe el diseño aprobado; no implementa código.

## Contexto y objetivo

El pipeline ya calcula rangos de escena antes de solicitar los recursos y puede sustituir un
vídeo fallido por una imagen. El límite superior actual de imágenes obliga a fragmentar escenas
que podrían sostener una imagen durante más tiempo, aumentando solicitudes, cambios visuales y
la probabilidad de agotar recursos únicos.

El objetivo es permitir imágenes de hasta 15 s sin relajar las protecciones de continuidad,
duración total del vídeo, deduplicación ni las reglas específicas de clips de vídeo.

## Decisión aprobada

### Límites por tipo de recurso

| Contrato | Mínimo | Máximo | Tratamiento |
| --- | ---: | ---: | --- |
| Imagen | 4 s | 15 s | Nuevo rango adaptable |
| Vídeo | 4 s | 7 s | Se mantiene sin cambios |
| Escena legacy/global | 8 s | 16 s | Se mantiene como límite heredado |

Los límites específicos por tipo tienen prioridad al validar y al aplicar el rango a una escena.
El rango global/legacy continúa siendo el fallback para configuraciones antiguas que no definen
límites específicos; no se debe reinterpretar como un nuevo rango de imagen.

### Validador de configuración

El validador debe:

1. aceptar `IMAGE_SCENE_DURATION_MIN=4` y `IMAGE_SCENE_DURATION_MAX=15` como configuración
   válida;
2. rechazar o corregir configuraciones de imagen fuera de los límites declarados;
3. conservar la comprobación `mínimo < máximo`;
4. conservar las reglas y defaults de `SCENE_DURATION_MIN/MAX` legacy;
5. conservar las reglas y defaults de `VIDEO_SCENE_DURATION_MIN/MAX` (4–7 s);
6. emitir mensajes que identifiquen el prefijo (`IMAGE`, `VIDEO` o legacy) para evitar que un
   ajuste de imágenes se confunda con un ajuste global.

La normalización debe ser determinista y no modificar otros parámetros del canal.

### Fallback de `MediaFetcher`

Cuando un recurso de vídeo no está disponible y el resultado efectivo es una imagen, el fallback
debe reconciliar la duración solicitada con el rango de imágenes 4–15 s:

- una escena de imagen entre 4 y 15 s se conserva como una sola escena;
- una escena de más de 15 s se divide en subescenas contiguas, sin huecos ni solapamientos;
- la división debe repartir la duración de forma equilibrada para que cada subescena quede dentro
  de 4–15 s siempre que el total lo permita;
- las subescenas conservan el orden temporal, el contexto textual y el vínculo con la solicitud
  original;
- cada subescena debe recibir un identificador de solicitud distinto para preservar la trazabilidad
  y las garantías existentes de deduplicación;
- una duración inferior a 4 s no se debe ocultar mediante una ampliación arbitraria: debe seguir
  la política existente de normalización de rangos y quedar cubierta por tests explícitos;
- el fallback no debe cambiar el rango de clips de vídeo ni convertir una imagen en un clip de
  vídeo artificial.

La reconciliación se aplica tanto al fallback de imagen por fallo de vídeo como a los caminos de
imagen que ya llegan a `MediaFetcher` con `scene_ranges`.

## Límites que deliberadamente no cambian

- `SCENE_DURATION_MIN/MAX` sigue protegiendo el algoritmo legacy/global y la compatibilidad con
  callers antiguos.
- `VIDEO_SCENE_DURATION_MIN/MAX` sigue siendo 4–7 s. Un vídeo no hereda el nuevo máximo de 15 s.
- No cambian las reglas de duración total, continuidad de timeline, una escena por asset lógico,
  deduplicación de URLs/IDs ni el uso de placeholder cuando se agotan los recursos.
- No se cambia el prompt ni se añade un default por canal. La configuración sigue fluyendo por
  defaults, configuración de canal y `config_bridge`.

## Riesgos y mitigaciones

### Riesgos inmediatos

- **Ritmo visual más lento:** una imagen de 15 s puede reducir la variedad percibida. Mitigar con
  métricas de retención y revisión visual de muestras, no reduciendo silenciosamente el rango.
- **Desfase entre capas:** validador, cálculo de rangos, fallback y editor podrían usar máximos
  distintos. Mitigar con constantes/configuración compartida y tests de contrato.
- **Falsa sensación de cobertura:** una imagen válida no garantiza que el recurso sea relevante o
  único. Mantener intactas las comprobaciones de calidad, contexto y deduplicación.

### Riesgo para un futuro algoritmo adaptativo

Un algoritmo posterior podría elegir la duración según retención, complejidad narrativa,
movimiento de cámara o disponibilidad de assets. Si aprende únicamente de la duración final,
podría confundir una escena larga por decisión editorial con una escena larga causada por falta de
recursos. Por ello:

- conservar la diferencia entre duración objetivo, duración reconciliada y duración efectiva del
  asset;
- registrar el motivo de cada división (`over_image_max`) y el tipo de recurso resultante;
- no convertir 15 s en un nuevo valor fijo obligatorio para todos los canales;
- diseñar el futuro algoritmo para consumir el intervalo `[min, max]` y señales de calidad, no solo
  un número global;
- reevaluar este contrato cuando exista suficiente histórico de retención y sustituciones.

## Pruebas necesarias para la implementación

Las pruebas deben añadirse antes o junto con el cambio de código y ejecutarse sin secretos ni
servicios externos.

### Validador

- acepta imagen 4–15;
- rechaza/corrige mínimo de imagen mayor o igual que máximo;
- rechaza/corrige máximos de imagen fuera del rango permitido;
- verifica que los límites legacy 8–16 y vídeo 4–7 no cambian;
- comprueba que la validación no muta configuraciones no relacionadas.

### Reconciliador de `MediaFetcher`

- imagen de 4 s, 15 s y valores intermedios: una sola escena;
- imagen de 15 s exactos: no se divide por error de redondeo;
- imágenes de más de 15 s: subescenas contiguas, equilibradas y dentro de 4–15 s;
- imagen inferior a 4 s: comportamiento explícito y estable según la política acordada;
- fallback de vídeo a imagen: usa el máximo de imagen, no el máximo de vídeo;
- IDs de solicitud únicos tras dividir;
- preservación de `start`, `end`, orden, contexto y deduplicación;
- compatibilidad del camino legacy cuando `scene_ranges` no se proporciona.

### Regresión de pipeline

- ejecución de los tests de rangos de escena y de `MediaFetcher`;
- prueba de continuidad del timeline y duración total;
- prueba de que no se relajan los límites globales ni de vídeo;
- prueba de que el fallback final a placeholder sigue disponible cuando no hay assets válidos.

## Criterios de aceptación

La implementación futura se acepta únicamente si:

- [ ] el contrato documentado 4–15 s para imágenes está reflejado en defaults/validator y en el
      fallback efectivo de `MediaFetcher`;
- [ ] las escenas de vídeo siguen limitadas a 4–7 s;
- [ ] los límites legacy/globales siguen funcionando para callers antiguos;
- [ ] no se generan huecos, solapamientos ni duraciones fuera de rango al reconciliar imágenes;
- [ ] se preservan deduplicación, trazabilidad y contexto de las solicitudes;
- [ ] existen tests unitarios y de regresión para todos los casos anteriores;
- [ ] la suite relevante pasa en un entorno sin credenciales externas;
- [ ] se revisa una muestra visual y se registra el resultado junto con métricas iniciales;
- [ ] no se han cometido secretos, tokens, credenciales ni artefactos de runtime.

## Revisión breve del documento

- **Alcance:** limitado a diseño y criterios; no prescribe implementación fuera de las capas
  afectadas.
- **Consistencia:** distingue explícitamente imagen, vídeo y límites legacy para evitar regresiones
  de compatibilidad.
- **Operabilidad:** incluye pruebas aisladas, regresión, observabilidad mínima y una fecha de
  revisión.
- **Pendiente antes de implementar:** acordar en los tests el comportamiento exacto de duraciones
  inferiores a 4 s, sin ampliar silenciosamente el rango aprobado.
