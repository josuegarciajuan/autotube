# ADR: Política efectiva de entrega por canal

**Estado:** Aceptada  
**Fecha:** 2026-08-31

## Contexto

La cadencia se aplicaba desde varias capas: perfiles globales, configuración
Python, `config_json`, políticas explícitas y estado de strikes. Los valores
duplicados podían contradecirse y un error de visibilidad de YouTube llegó a
bloquear operativamente ESR aunque el canal seguía activo.

## Decisión

La política efectiva se resolverá por canal. La identidad, cuenta y proyecto
se obtienen de la BD; la configuración pasa por `config_bridge`; la cadencia
usa el perfil global como límite de seguridad y la política explícita del canal
como ritmo solicitado. Los contadores históricos no equivalen a un bloqueo
activo.

El orden de aplicación es:

1. bloqueo confirmado o hold manual;
2. límites de seguridad globales;
3. política explícita del canal;
4. perfil global;
5. configuración y defaults.

Ninguna capa inferior puede relajar una restricción superior.

## Estado de YouTube

`private`, `scheduled`, `unknown`, `error` y `LOGIN_REQUIRED` no prueban una
eliminación. Solo `removed` confirmado por dos observaciones independientes
puede iniciar un bloqueo automático. La evidencia histórica se conserva.

## Alternativas descartadas

### Un único perfil global para todos los canales

Descartado: no representa los ritmos editoriales personalizados y obliga a
introducir excepciones hardcodeadas.

### Borrar los contadores al desbloquear

Descartado: destruye trazabilidad y dificulta analizar reincidencias.

### Confiar en una única respuesta de watch page/yt-dlp

Descartado: estados privados, errores transitorios y retrasos de indexación
pueden parecer eliminaciones.

## Consecuencias

- Añadir un canal no requiere modificar código de enforcement.
- Las políticas son auditables y se pueden mostrar en el panel.
- La migración legacy debe ser gradual para evitar perder límites.
- Los scripts operativos deben resolver selectores desde la BD y no asumir IDs.

## Acciones

1. [x] Introducir resolver central y pruebas iniciales.
2. [x] Corregir el falso bloqueo operativo de ESR.
3. [ ] Parametrizar scripts operativos históricos.
4. [ ] Retirar lectores legacy tras una ventana de dual-read.
5. [ ] Añadir gate CI contra nuevos hardcodes operativos.
