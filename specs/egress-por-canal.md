# Egress aislado por canal — invariante y arquitectura

> **Invariante (NO relajar):** todo canal marcado como gestionado debe realizar
> **todas** sus interacciones con YouTube/Google desde su gateway/IP de salida.
> Si el gateway no está disponible, la IP no es la esperada o el perfil está
> incompleto, la operación se **bloquea**. Nunca se cae a la IP del servidor.

## 1. Objetivo

Evitar que un strike/ban de un canal arrastre a los demás por compartir IP.
Cada canal nuevo tiene su propia identidad de salida, cuenta Google, proyecto
GCP y perfil de navegador. Los canales antiguos pueden seguir como están.

## 2. Unidad de aislamiento

**1 canal = 1 gateway = 1 IP de salida = 1 cuenta Google = 1 proyecto GCP = 1
perfil de Chromium.** No se comparte ninguno de esos elementos entre canales.

Si dos canales comparten host pero deben aislarse entre sí, el gateway debe
usar un proxy/IP distinta por canal (`playwright_proxy` + proxy explícito en
`requests`, `httplib2` y `yt-dlp`). Mientras compartan IP, quedan aislados del
resto de canales pero no entre ellos.

## 3. Fuentes de verdad

| Dato | Dónde vive | Notas |
|---|---|---|
| "Es gestionado" | `channels.egress_managed = 1` (DB) **y/o** `config/egress_agents.json` | Unión OR. La DB es durable: si el JSON se pierde, sigue gestionado. |
| URL/token del agente | `config/egress_agents.json` (gitignored) en el servidor principal | Secreto. No va a `config_json`. |
| IP esperada | `config/egress_agents.json` + `channels.egress_expected_ip` | Deben coincidir. |
| OAuth (`*.pickle`) | Solo en el gateway (`/data/tokens`) | Nunca en el servidor principal. |
| `client_secret_*.json` | Solo en el gateway (`/data`) | Nunca en el servidor principal. |
| Perfil de Chromium | Solo en el gateway (`/data/tokens/<cuenta>_browser_profile`) | Un perfil por cuenta. |

`api/services/egress_registry.py` resuelve el perfil y lanza
`EgressConfigurationError` si falta cualquier campo. `is_egress_managed`
devuelve `True` aunque el perfil esté incompleto, para que el caller aplique
fail-closed.

## 4. Arquitectura

```
Servidor principal (autotube)
  · generación, DB, panel, planificación
  · egress_registry / egress_client
  · NUNCA habla con YouTube/Google de un canal gestionado
        │  HTTP + X-Agent-Token (tailnet)
        ▼
Nodo del canal (contenedor aislado)
  · egress-agent
  · Chromium/Playwright + XFCE + X11VNC
  · OAuth, client_secret, perfil, staging
  · sale por la IP del nodo (o su proxy)
```

El gateway es un **servicio systemd persistente**, no un job efímero de
SuperServer. Los jobs distribuidos no deben manejar credenciales OAuth ni
perfiles de navegador.

## 5. Puntos de interacción cubiertos

Para un canal gestionado, deben delegarse o bloquearse explícitamente:

- Subida long-form y shorts (stage → upload → cleanup).
- `publishAt`, privacidad, título, descripción, thumbnail.
- Estadísticas: Data API, Analytics API, scraper público (vistas/likes/subs).
- Playlists, comentarios, respuestas, colaboraciones.
- Acciones de YouTube Studio: marcar contenido IA, end screens, hold, link,
  studio scan, comentarios.
- Reconciliación de visibilidad: yt-dlp, RSS, watch page, `i.ytimg.com`.
- OAuth: URL, intercambio de código, refresco de token.
- Lecturas Google: Google News/Suggest si el canal las usa.
- Descarga de clips/audio de YouTube (yt-dlp).

Cualquier ruta no delegada debe fallar con `EgressAgentUnavailableError` o
`EgressConfigurationError`; nunca ejecutar la versión local.

## 6. Contrato fail-closed

| Escenario | Resultado |
|---|---|
| Gateway inaccesible | Operación bloqueada; job en `awaiting_egress`; alerta. |
| IP de salida ≠ `expected_ip` | Bloqueo + `egress_down_<slug>=1` + alerta crítica. |
| `expected_ip` ausente | Bloqueo (no se acepta "cualquier IP"). |
| URL/token ausentes | `EgressConfigurationError`; bloqueo. |
| El JSON secreto desaparece pero la DB dice gestionado | Bloqueo (no se vuelve local). |
| Credenciales locales en el servidor | Arranque marca `egress_status=error` + alerta. |
| `egress_down_<slug>=1` | Toda operación se bloquea antes de la red. |

## 7. Staging y limpieza

- El servidor transfiere el mp4 a `/data/staging/<ref>/video.mp4`.
- El gateway sube y el servidor llama a `/cleanup` con la ruta staged.
- `/cleanup` solo acepta rutas dentro de `staging_dir`; borra el vídeo, sus
  thumbnails y el directorio vacío.
- Purga por TTL (`staging_ttl_hours`, por defecto 6 h).
- Cuotas: `staging_max_gb` (por canal), `min_free_gb` (global) y `max_body_mb`.
- Si el disco baja del mínimo, `/stage` responde 507 y el servidor difiere.

## 8. Alta de un canal (resumen)

1. Elegir IP de salida y cuenta Google nuevas; proyecto GCP propio.
2. Instalar el contenedor en el nodo con `install.sh`.
3. Copiar la entrada a `config/egress_agents.json` y marcar
   `channels.egress_managed=1`.
4. Abrir VNC por túnel SSH; verificar `api.ipify.org` = IP esperada.
5. Crear Gmail + canal de YouTube + proyecto GCP + OAuth client **desde ese
   navegador**; descargar `client_secret_<slug>.json`.
6. Copiar el `client_secret` solo al nodo (`/data/client_secret_<slug>.json`).
7. OAuth vía agente (`auth-start`/`auth-code`); el token queda solo en el nodo.
8. Login en YouTube Studio en el mismo perfil.
9. `verify_egress --deep` en verde; sin credenciales locales en el servidor.
10. Activar con cadencia baja y monitorizar el primer ciclo.

## 9. Regla para cambios futuros

- Toda llamada nueva a YouTube/Google debe pasar por el router/gateway. Si aún
  no está delegada, se bloquea con `fail_closed_if_managed`.
- No añadir rutas locales para canales gestionados, ni siquiera "temporales".
- No mover tokens/perfiles al servidor principal.
- Si se comparte un nodo entre canales, añadir proxy por canal antes de
  considerarlos aislados entre sí.
- Los tests estáticos y los de contrato deben seguir cubriendo cada operación.

## 10. Referencias

- `deploy/egress-node/README.md` — build/install/verify del contenedor.
- `docs/runbook-egress-node.md` — alta de los 2 canales y réplica.
- `specs/egress-aislado.md` — arquitectura original del agente.
- `specs/onboarding-canal-egress.md` — checklist de onboarding.
