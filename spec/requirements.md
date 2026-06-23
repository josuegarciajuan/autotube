# Autotube — Requisitos del plan de mejora

## Requisitos funcionales

1. **Una generación = un registro**: al pulsar "Generar Video" desde el panel, solo debe crearse 1 registro en `videos`, no 3-4.
2. **Registro trackeado con yt_video_id**: el registro que la API crea y actualiza debe recibir `yt_video_id`/`yt_url` tras subida exitosa a YouTube.
3. **Duración basada en config del canal**: el guion debe respetar `VIDEO_OPTIMAL_DURATION_MINUTES` del canal (sin hardcodear).
4. **Pollo AI funcionando**: usar la API oficial con `x-api-key`, portando el cliente de lamamionline-control/pollo_text_to_image.py.
5. **Thumbnails virales**: imágenes generadas con caras humanas de sorpresa, específicas al contenido, badge 4K al doble, sin doble-composición.
6. **Títulos virales**: metadatos con más curiosity-gap y fórmulas de clickbait ético.
7. **Progreso granular**: feedback detallado en el panel durante todas las fases.
8. **Limpieza mp4 + embed**: borrar mp4 local tras subida OK; reproducir video de YouTube embebido en el panel.

## Requisitos no funcionales
- Seguridad: sin regresiones en auth YouTube OAuth, sin leaks de cookies/secrets en logs.
- Compatibilidad: el modo CLI standalone (`main.py run`) debe seguir funcionando.
- Rendimiento: la generación no debe ralentizarse respecto a la versión actual.
