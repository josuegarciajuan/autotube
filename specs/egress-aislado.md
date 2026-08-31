# Egress aislado por cuenta (agente egress en VPS dedicado)

> **Invariante egress (NO relajar sin justificación):**
> Cualquier interacción con YouTube/Google de un canal **con IP/VPS externo
> configurado** DEBE salir por esa IP (agente egress del VPS). Solo los canales
> SIN egress externo configurado usan la IP del server principal. **Nunca**
> mezclar la IP del server con canales que tengan egress propio, ni usar la IP
> del server como salida de un canal gestionado. El incumplimiento relaciona
> canales entre sí y expone la IP del server a YouTube.

## Objetivo
Aislar por completo (IP + huella de dispositivo) canales adquiridos/creados que
no deben relacionarse con el resto de canales ni con la IP del server principal.

## Arquitectura
```
[ Server principal (autotube) ]            [ VPS de egreso (194.233.67.64) ]
  · generación, scheduling, DB, UI          · agente egress (egress_agent) :9101
  · NUNCA toca YouTube para canales          · navegador Playwright + yt-dlp + API
    gestionados: delega en el agente          · TODO su egress sale por la IP
                                              · residencial (58.68.169.25, Madrid)
```

- El server alcanza al agente por `http://194.233.67.64:9101` (puerto abierto solo
  para la IP del server en ufw) con token `X-Agent-Token`.
- El agente sale por la **IP residencial** `58.68.169.25` (Madrid, ES) vía proxy
  HTTP configurado en `playwright_proxy` + `HTTP_PROXY/HTTPS_PROXY` (systemd).
- Verificación: `/egress-check` debe devolver `58.68.169.25`, nunca la IP del
  server ni la del VPS datacenter.

## Datos fijos (no versionar)
| Dato | Valor | Dónde |
|---|---|---|
| VPS | `194.233.67.64` (root) | `.env`: `VPS_SSH_HOST/USER/PASS` |
| Directorio del repo en el VPS | `/opt/autotube` | — |
| Proxy residencial (HTTP) | `58.68.169.25:59100` (tracatrack) | `.env`: `EGRESS_PROXY1` |
| IP de egreso esperada | `58.68.169.25` (Madrid) | — |
| Token del agente | (generado) | `config/egress_agents.json` + `/opt/agent_config.json` |

## Despliegue remoto automático
- **`scripts/apply_changes.sh` Step 4**: tras aplicar al server, hace
  `git pull --ff-only` en `/opt/autotube` del VPS + `systemctl restart egress-agent`.
  Fail-open: si el VPS no responde, avisa y no bloquea el deploy local.
- **Hook `post-merge`** (en `main`): ejecuta `apply_changes.sh` automáticamente
  tras cada merge, con `flock` para evitar solapamiento. Log en
  `logs/auto_deploy.log`.
- El VPS sigue `origin/main`: para que el agente reciba cambios, `main` debe
  estar pusheado a `origin`.

## Servicios systemd en el VPS
- `egress-agent.service` — agente (Python 3.10 venv `/opt/autotube/.venv`, config `/opt/agent_config.json`).
- `xvfb.service` (:1), `xfce-desktop.service`, `vnc.service` (:5900), `novnc.service` (:6080) — entorno visual para crear la cuenta.
- Chromium del entorno visual sale por la IP residencial (`/opt/navegador_canal6.sh`).

## Onboarding de un canal gestionado
1. Crear la cuenta Google/YouTube desde el entorno visual (noVNC), con IP
   residencial + teléfono nuevo (paso manual, lo deja el operador).
2. Configurar `google_account` en `agent_config.json` del VPS.
3. OAuth (`auth-start`/`auth-code` vía agente) + `yt_browser_login` en el VPS.
4. `check-egress` en verde en el panel → activar el canal.

## Fail-closed
Un canal listado en `config/egress_agents.json` NUNCA cae al egress local: si el
agente no responde, la operación lanza `EgressAgentUnavailableError` (nunca filtra
la IP del server). Los caminos aún no delegados (stats/playlists/comments-API/
metadata) están bloqueados con `fail_closed_if_managed` hasta delegarse.
