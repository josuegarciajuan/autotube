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

## Endurecimiento (ago 2026)
Además del monitor y la guardia por-operación, el mecanismo incluye:

- **`expected_ip` OBLIGATORIO** (`egress_agent/server.py::_require_egress`): un
  agente sin `expected_ip` configurado BLOQUEA todas las operaciones (fail-closed
  por misconfig). Ya no existe el skip silencioso. Para desarrollo/test hay un
  opt-out explícito `egress_verify: false` en `agent_config.json` o env
  `AGENT_EGRESS_VERIFY=false` — **NUNCA** en producción.
- **`/healthz`** (agente): una sola llamada devuelve `{ok, slug, token_valid,
  expected_ip, egress_ip, egress_ok}` para monitorización/CI.
- **`/egress-check-browser`** (agente): verifica el egress REAL del navegador.
  Lanza una página headless vía Playwright POR el proxy residencial y devuelve
  `{browser_ip, expected_ip, match, webrtc_disabled}`. Detecta fugas de
  capa-browser (WebRTC/IP) que el probe curl no ve. On-demand (no cada 60 s).
- **`scripts/verify_egress.py`** (server): gate end-to-end. Para cada canal
  gestionado comprueba alcanzabilidad (`/healthz`) + IP curl (`/egress-check`) +
  egress del navegador (`/egress-check-browser`). Exit 0 si todo en verde, 1 si
  hay fuga/IP caída/expected_ip ausente. Uso: `python3 scripts/verify_egress.py`
  (`--skip-browser` omite la prueba costosa; `--slug canal6` limita a uno).
- **Test estructural anti-contaminación** (`tests/test_egress_structure.py`):
  impide que `tokens/<slug>.pickle` o `client_secret_<slug>.json` de un canal
  gestionado existan en el server principal, y que `egress_agents.json` (con el
  token) se versionara.

## Alta posterior a crear la cuenta (H7 — checklist operativo)
Tras crear la cuenta Google/YouTube desde el entorno visual (ver
`onboarding-canal-egress.md`), completar en orden:

1. **Proyecto GCP + OAuth client** bajo la cuenta Google NUEVA (no tracatrack ni
   burrianacasa). API: YouTube Data API v3 + Analytics API habilitadas. Descargar
   el JSON del client → `client_secret_canal6.json`.
2. **Desplegar el client_secret en el VPS**:
   `scp client_secret_canal6.json root@194.233.67.64:/opt/autotube/config/`
   (el agente lo resuelve vía `client_secret_path`).
3. **Setear `google_account`** en `/opt/agent_config.json` (la cuenta creada) y
   verificar que `expected_ip: "58.68.169.25"` sigue presente. Reiniciar:
   `systemctl restart egress-agent`.
4. **OAuth vía agente** (IP residencial): en el panel o API, `auth-start` →
   abrir la URL en el navegador del VPS → `auth-code`. El token queda SOLO en el
   VPS (`tokens/canal6.pickle`).
5. **Gate de egress en verde**:
   `python3 scripts/verify_egress.py --slug canal6` → debe imprimir `PASS`.
   También `GET /api/channels/{id}/check-egress` en el panel (managed=true,
   agent_ip=58.68.169.25).
6. **Activar el canal** (planificación normal, nunca en ráfaga).

> Verificación de fuga: `curl -s https://api.ipify.org` DENTRO del Chromium del
> VPS (`/opt/navegador_canal6.sh`) debe mostrar `58.68.169.25`, nunca la IP del
> VPS ni la del server.

## Notas operativas (H8)
- **La IP residencial (Geonix) expira.** Si cae/caduca, el monitor marca
  `egress_down_canal6=1` + alerta `egress_ip_down`, y TODA operación del canal se
  bloquea (fail-closed) hasta renovarla en Geonix. El agente también rechaza por
  sí solo cada operación si su IP de salida no es la esperada.
- **Carga del VPS:** mantener un ojo en la carga (históricamente alta ~25-30 en
  194.233.67.64). Una carga sostenida puede degradar subidas largas.
- **ISP de la IP:** la IP residencial geolocaliza en Madrid pero reporta ISP
  "M247 Europe SRL" (proveedor de hosting, no un ISP residencial clásico). Es una
  consideración de fingerprint (Google podría verla como IP de datacenter). No
  bloquea, pero tenerlo presente.
- **Cada subida de un canal gestionado pasa por `/stage` + `/upload`** del agente:
  el server transfiere el mp4 al VPS y el VPS sube a YouTube desde la IP
  residencial con la programación del server.
