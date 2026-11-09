# Runbook: alta de un canal con gateway propio

Aplica a los 2 canales nuevos. Los 4 actuales no se migran.

## 0. Decisiones fijas de esta fase

- Nodo: `encuestaCitas` (`vps649560`), IP `151.80.142.93`.
- Un contenedor por canal; mismo host, distinto puerto/perfil.
- Cuenta Google, proyecto GCP y perfil de navegador nuevos por canal.
- Alta **asistida**: el operador crea Gmail/YouTube/GCP desde TightVNC.
- Contenedor aislado: sin DB del servidor, sin correo/web del host.

## 1. Preparar el nodo

```sh
ssh root@100.90.30.60
cd /opt/autotube   # o clonar el repo
git pull --ff-only
IMAGE=autotube-egress:1 ./deploy/egress-node/build.sh
```

Comprobar disco:

```sh
df -h /
```

Debe haber >= 8 GB libres. Si no, liberar antes de continuar.

## 2. Instalar el gateway del primer canal

```sh
./deploy/egress-node/install.sh \
  --slug canal6 \
  --agent-port 9106 \
  --vnc-port 5956 \
  --expected-ip 151.80.142.93 \
  --google-account PENDIENTE
```

Guardar la salida:

- entrada JSON para `config/egress_agents.json`;
- comando de túnel SSH;
- password VNC.

## 3. Registrar en el servidor principal

1. Copiar la entrada a `config/egress_agents.json` (gitignored, permisos 600).
2. Marcar el canal en la DB:

```sql
UPDATE channels
SET egress_managed = 1,
    egress_expected_ip = '151.80.142.93',
    egress_status = 'provisioning',
    egress_mode = 'gateway',
    egress_label = 'encuestaCitas'
WHERE slug = 'canal6';
```

3. Reiniciar la API para que el registro se recargue:

```sh
bash scripts/apply_changes.sh
```

4. Comprobar que el monitor no marca error:

```sh
curl -s localhost:8000/api/system/egress-status || true
```

## 4. Abrir el escritorio VNC

En tu máquina:

```sh
ssh -L 5956:127.0.0.1:5956 root@100.90.30.60
```

TightVNC Viewer → `localhost:5956`, password del instalador.

Dentro del escritorio:

```sh
/app/deploy/egress-node/navegador.sh
```

Verificar en el navegador: `https://api.ipify.org` debe mostrar
`151.80.142.93`. Si no coincide, **parar**.

## 5. Crear la identidad (manual asistido)

En ese Chromium, con el perfil del canal:

1. Crear Gmail nuevo con teléfono nuevo (nunca usado en otras cuentas).
2. Activar 2FA y recuperación.
3. Crear el canal de YouTube con esa cuenta.
4. Crear un proyecto GCP **bajo esa misma cuenta**.
5. Habilitar YouTube Data API v3, Analytics API y Reporting API (si se usa).
6. Crear OAuth client tipo Desktop.
7. Descargar el JSON del client como `client_secret_canal6.json`.

No abrir ninguna otra cuenta Google en ese perfil.

## 6. Desplegar el secreto solo en el nodo

```sh
scp client_secret_canal6.json root@100.90.30.60:/opt/autotube-egress/canal6/
ssh root@100.90.30.60 '
  mv /opt/autotube-egress/canal6/client_secret_canal6.json \
     /opt/autotube-egress/canal6/client_secret_canal6.json
  chown 1000:1000 /opt/autotube-egress/canal6/client_secret_canal6.json
  chmod 600 /opt/autotube-egress/canal6/client_secret_canal6.json
  systemctl restart egress-canal6.service
'
```

Nunca copiar el `client_secret` al servidor principal.

## 7. OAuth vía agente

En el panel o API del servidor principal:

1. `POST /api/channels/{id}/auth-start` → devuelve URL.
2. Abrir la URL **en el Chromium del VNC** (misma IP).
3. Autorizar; copiar el `code`.
4. `POST /api/channels/{id}/auth-code` con el código.
5. `GET /api/channels/{id}/auth-status` debe estar OK.

El token queda en `/opt/autotube-egress/canal6/tokens/canal6.pickle`.

## 8. Login de YouTube Studio

En el mismo Chromium del VNC:

1. Abrir `https://studio.youtube.com`.
2. Iniciar sesión con la cuenta del canal.
3. Comprobar que carga el canal correcto.

## 9. Verificación final

En el servidor principal:

```sh
python3 scripts/verify_egress.py --slug canal6 --skip-browser
python3 scripts/verify_egress.py --slug canal6
```

Debe salir `PASS` y la IP `151.80.142.93`. Además:

- `GET /api/channels/{id}/check-egress` → `managed: true`, IP correcta.
- No debe existir `tokens/canal6.pickle` ni
  `config/client_secret_canal6.json` en el servidor principal.
- `GET /api/system/channel-restrictions` para ese canal, sin restricciones.

## 10. Activar y observar

1. Configurar planificación con cadencia baja (1 long + 1 short/día).
2. Activar el canal.
3. Primer ciclo: subida privada + `publishAt`, thumbnail, marcado IA, end
   screens, stats, playlist.
4. Revisar logs del contenedor:

```sh
journalctl -u egress-canal6 -n 100 --no-pager
docker logs egress-canal6 --tail 100
```

## 11. Repetir para el segundo canal

Mismo host, pero:

- `--slug canal7`;
- `--agent-port 9107`;
- `--vnc-port 5957`;
- cuenta Google, GCP y perfil nuevos.

Los dos canales comparten la IP del nodo. Quedan aislados de los 4 actuales,
pero no entre sí. Si más adelante se quiere aislamiento total, añadir un proxy
por canal (`playwright_proxy` + proxy explícito en `requests`/`googleapiclient`/
`yt-dlp`) y actualizar `expected_ip`.

## 12. Actualizaciones y réplica en otro nodo

Actualizar:

```sh
cd /opt/autotube && git pull --ff-only
IMAGE=autotube-egress:1 ./deploy/egress-node/build.sh
systemctl restart egress-canal6.service egress-canal7.service
```

Replicar en otro nodo:

1. Instalar Docker/systemd y clonar el repo.
2. Construir/cargar la imagen.
3. Ejecutar `install.sh` con el nuevo `--expected-ip`.
4. Copiar la nueva entrada a `config/egress_agents.json`.
5. Alta asistida del canal desde el VNC de ese nodo.

## 13. Incidentes

| Síntoma | Acción |
|---|---|
| `egress_down_<slug>=1` | Revisar `healthz`, red/proxy, `expected_ip`; no operar hasta verde. |
| Subida atascada en `awaiting_vps_upload` | Logs del contenedor; revisar disco y token; reintentar. |
| Disco alto | `docker system df`, purgar staging, revisar TTL. |
| Chromium/VNC no arranca | `docker logs`; reconstruir imagen; reiniciar servicio. |
| Cambio de IP | Actualizar `expected_ip` en el nodo y en `egress_agents.json`; reiniciar. |

## 14. Alta asistida con software

El alta de un canal nuevo se reduce a un comando (desde el servidor principal):

```sh
python3 scripts/provision_egress_channel.py \
  --slug canal7 \
  --name "Canal 7" \
  --ssh-host 100.90.30.60 \
  --expected-ip 151.80.142.93 \
  --agent-port 9107 \
  --vnc-port 5957 \
  --google-account canal7
```

Hace:

- crea/marca el canal con `egress_managed=1`;
- instala/actualiza `egress-canal7.service` en el nodo;
- escribe la entrada en `config/egress_agents.json` (0600);
- guarda el password VNC en `/root/.config/autotube/egress-canal7-vnc.txt`;
- imprime el túnel SSH y el checklist.

Después, el operador entra por TightVNC y crea Gmail/YouTube/GCP. Cuando tenga
el `client_secret`:

```sh
python3 scripts/deploy_channel_oauth.py \
  --slug canal7 --file client_secret_canal7.json
```

Y completa OAuth/Studio siguiendo la sección 7-8.

## 15. Actualización de todos los gateways

Tras un cambio de código:

```sh
python3 scripts/sync_egress_gateways.py
```

Reconstruye la imagen, la transfiere a cada nodo, reinicia `egress-<slug>` y
verifica `healthz`/IP. Si falla un gateway, no se considera actualizado.

## 16. Gate antes de operar

```sh
python3 scripts/audit_egress_coverage.py
python3 scripts/verify_egress.py --slug canal7 --deep
```

Ambos deben estar en verde. `--deep` además prueba `get_channel_info` vía
gateway y comprueba que no hay credenciales locales del canal en el servidor.
