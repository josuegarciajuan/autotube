# Onboarding de un canal gestionado (VPS + IP residencial)

> Documento operativo para dar de alta un canal cuyo egress a YouTube sale por un
> VPS dedicado con IP residencial (aislamiento total del server principal).
> Lee antes `specs/egress-aislado.md` (invariante + arquitectura).

---

## 1. Cómo funciona un canal gestionado (comportamiento esperado)

Un canal está en `config/egress_agents.json` → es **gestionado**. Desde ese
momento el server principal **NUNCA** toca YouTube/Google para ese canal:

| Operación | Qué ocurre |
|---|---|
| **Subida** (long + shorts) | El vídeo se genera aquí; el server lo **transfiere** al VPS (`/stage`, estados `transferring → awaiting_vps_upload`), y el VPS sube a YouTube (`/upload`) **con la IP residencial** y la programación del server (título, desc, tags, publishAt). |
| **Stats** ("Recolectar stats") | El server pide `collect_stats` al agente (IP residencial); el agente recolecta y devuelve el payload; el server lo guarda en su DB. |
| **Playlists / Comentarios-API / Metadata / Channel** | Cada operación se delega al agente (`/api/call`); el server aplica el bookkeeping local. |
| **Navegador (Studio)**: auto-marcado IA, end screens, link, hold, scan, comentarios, colab | Se delegan al agente (`/browser/action`) → el navegador corre en el VPS con la IP residencial. |
| **Reconciliación / RSS / watch pages / sweep / OAuth** | Vía agente. |
| **Liveness de la IP** | `egress_monitor` comprueba cada **60 s** que el agente sale por `58.68.169.25`. Si caduca/cae → alerta `egress_ip_down` + `egress_down_<slug>`; además el **propio agente** rechaza cada operación si su IP no es la esperada (fail-closed). |

**Invariantes:** nunca se usa la IP del server para un canal gestionado; si el
agente está caído o la IP no es la esperada, la operación se bloquea (nunca cae
a la IP del server, nunca sale por una IP equivocada).

---

## 2. Prerrequisitos antes de dar de alta

- [ ] El **VPS** (`194.233.67.64`) corre `egress-agent` (systemd `egress-agent.service`) con su `agent_config.json` (incluye `expected_ip`).
- [ ] La **IP residencial** (`58.68.169.25`, proveedor **Geonix**) está activa → `/egress-check` devuelve esa IP.
- [ ] El canal está en `config/egress_agents.json` con `{url, token, expected_ip, egress_label}`.
- [ ] El **entorno visual (noVNC)** está activo (services `xvfb`/`xfce-desktop`/`vnc`/`novnc`) y `/opt/navegador_canal6.sh` sale por la IP residencial.
- [ ] Tienes un **teléfono nuevo** (eSIM) para verificar la cuenta Google (nunca usado en otras cuentas).

---

## 3. PASO 0 — Entrar a la sesión gráfica YA con la IP (crear la cuenta primero)

> **Orden crítico:** primero se crea la cuenta Google + YouTube desde el entorno
> visual del VPS (con la IP residencial y el teléfono nuevo), y **después** se da
> de alta el canal en el panel. Nunca al revés.

Desde tu máquina (túnel SSH → noVNC):

```bash
ssh -L 6080:localhost:6080 root@194.233.67.64
# pass SSH: (ver .env VPS_SSH_PASS)
```
Abre en tu navegador: **`http://localhost:6080/vnc.html`** → pass VNC: **`canal6madrid`**

En el escritorio XFCE, ejecuta el navegador aislado (sale por la IP residencial):

```bash
/opt/navegador_canal6.sh
```

**Verifica la IP** (en la barra de direcciones de ese Chromium): abre
`https://api.ipify.org` → debe mostrar **`58.68.169.25`** (nada de la IP de tu
oficina/casa/server).

Ahora crea:
1. **Gmail** (cuenta Google nueva) en `accounts.google.com`, usando el **teléfono nuevo** para la verificación.
2. **Canal de YouTube** con esa cuenta (elige el nombre/handle del canal).
3. **No abras ninguna otra cuenta** en ese navegador.

---

## 4. PASO 1 — Dar de alta el canal en el panel

Con la cuenta Google creada y el canal de YT existente:

1. **Panel** → Canales → **Nuevo Canal**:
   - Nombre, `slug` (p. ej. `canal6`), `youtube_handle`, **`google_account`** (la cuenta creada).
2. Asegúrate de que el `slug` **coincide** con la clave en `config/egress_agents.json`.
3. **Sync Python** (`POST /api/channels/{id}/sync-config`) para cargar la config.

### Detalles que conviene pulir al dar de alta (checklist)
- [ ] `google_account` bien escrito (debe coincidir con el perfil de navegador del VPS).
- [ ] El `slug` coincide con `egress_agents.json` (si no, `is_egress_managed` da False y el canal NO delegaría).
- [ ] `client_secret_{slug}.json` del proyecto GCP **nuevo** de la cuenta (desplegado en el VPS).
- [ ] La cuenta tiene 2FA/recuperación configurada (nunca depender solo del vendedor/email).
- [ ] El teléfono de verificación es el nuevo (no compartido).

---

## 5. PASO 2 — OAuth (vía agente)

- `POST /api/channels/{id}/auth-start` → devuelve la URL de OAuth.
- Ábrela **en el navegador del VPS** (el de la sesión gráfica que sale por la IP residencial).
- Autoriza, copia el `code`, y `POST /api/channels/{id}/auth-code` con el código.
- El intercambio lo hace el agente (IP residencial). Verifica con `/auth-status`.

## 6. PASO 3 — Login del navegador en el VPS

En el VPS, con el perfil del canal, iniciar sesión en YouTube Studio para que el
auto-marcado IA / end screens funcionen:

```bash
# en el VPS, en la sesión gráfica con la IP residencial
/opt/navegador_canal6.sh   # abre Chromium con la IP
# inicia sesión en studio.youtube.com con la cuenta del canal
```

(O alternativamente el flujo `yt_browser_login` en el VPS con la IP puesta.)

## 7. PASO 4 — Verificación final

- **Panel** → canal → botón **"Verificar egress"** → debe mostrar `58.68.169.25` (Madrid) y **no** "FUGA".
- `GET /api/channels/{id}/check-egress` → `managed: true`, `agent_ip: 58.68.169.25`.
- En el server: `egress_down_canal6 == 0` (monitor sano).
- Prueba una **subida de prueba** y comprueba que el vídeo pasa por `awaiting_vps_upload` → `uploaded` (vía VPS).

## 8. PASO 5 — Activar la operación

- Configura la **planificación** (pacing normal, nunca arrancar en ráfaga).
- Activa el canal (`active=1`).
- Monitoriza el primer ciclo: subida, stats, auto-marcado IA, end screens, playlist.

---

## 9. Si falta algo al dar de alta (señales y qué hacer)

| Síntoma | Causa probable | Solución |
|---|---|---|
| El canal no delega (se comporta local) | `slug` no está en `egress_agents.json` o `google_account` mal | Añadir/ corregir `egress_agents.json` + sync |
| "IP residencial no verificada / caducada" | La IP de Geonix venció/cayó | Renovar en Geonix; el monitor se recupera solo |
| `egress_down_<slug}=1` + alerta | Agente inalcanzable o IP equivocada | Verificar VPS/agente/IP; revisar `expected_ip` |
| La subida se queda en `awaiting_vps_upload` | El agente no pudo subir | Logs del VPS; reintentar; revisar token/cuota |
| No aparece la sesión gráfica | services `novnc` caídos | `systemctl restart novnc` en el VPS |

---

## Datos fijos (no versionar)

| Dato | Valor |
|---|---|
| VPS | `194.233.67.64` (root) |
| Repo en VPS | `/opt/autotube` |
| Proxy residencial | `58.68.169.25:59100` (tracatrack) — **Geonix** |
| IP de egreso esperada | `58.68.169.25` (Madrid) |
| noVNC | `http://localhost:6080/vnc.html` (pass `canal6madrid`, por túnel SSH) |
| Navegador aislado | `/opt/navegador_canal6.sh` |
