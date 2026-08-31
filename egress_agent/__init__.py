"""Egress agent — tráfico aislado por cuenta Google.

Corre en una VPS dedicada por cuenta (o localmente en el server para pruebas).
Expone un mini servidor HTTP autenticado que el server principal (autotube)
invoca para TODO el egress a YouTube/Google de un canal gestionado:

    - /browser/{action}  : Studio / watch page (Playwright) en la VPS.
    - /upload            : sube un vídeo + thumbnail a YouTube desde la VPS.
    - /api/call          : operaciones googleapiclient (Data API + Analytics).
    - /ytdlp             : yt-dlp / RSS / scraping 0-cuota desde la VPS.
    - /fetch             : GET genérico (watch pages, feeds) desde la VPS.
    - /auth/*            : flujo OAuth desde la IP de la VPS.
    - /egress-check      : verifica la IP real de salida de la VPS.

La IP de salida NO la decide este código: la decide la configuración de red de
la VPS (ruta por defecto vía túnel a la IP residencial, o la IP del propio VPS
si no se configura túnel). Fail-closed: si la VPS no tiene salida, el agente no
puede contactar con YouTube y la operación falla — nunca "cae" a la IP del
server principal.
"""
