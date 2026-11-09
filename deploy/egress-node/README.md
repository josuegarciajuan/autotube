# Gateway egress por canal (nodo replicable)

Contenedor que ejecuta, por canal:

- `egress-agent` (API HTTP con token) para subir, stats, playlists, comentarios,
  OAuth y acciones de YouTube Studio.
- Escritorio XFCE + Chromium accesible por **TightVNC** (túnel SSH).
- Staging temporal: recibe el mp4, lo sube y el servidor ordena `/cleanup`.

Reglas duras:

1. Un contenedor por canal y por IP de salida. No compartir perfil ni cuenta.
2. Los secretos (`agent_config.json`, `client_secret_*`, `tokens/`, perfil de
   Chromium) viven solo en `/opt/autotube-egress/<slug>/` del nodo.
3. El contenedor no monta la DB del servidor principal (`AUTOTUBE_AGENT_MODE=1`).
4. VNC solo en `127.0.0.1`; se accede por túnel SSH, nunca expuesto a Internet.
5. Si el gateway cae o la IP no es la esperada, el servidor bloquea el canal
   (fail-closed). No hay fallback a la IP del servidor.

## Construir la imagen

En una máquina con Docker (o en el propio nodo):

```sh
git pull --ff-only
IMAGE=autotube-egress:1 ./deploy/egress-node/build.sh
```

Si se construye fuera del nodo, transferirla:

```sh
docker save autotube-egress:1 | ssh root@NODO docker load
```

## Instalar un canal

En el nodo, como root:

```sh
./deploy/egress-node/install.sh \
  --slug canal6 \
  --agent-port 9106 \
  --vnc-port 5956 \
  --expected-ip 151.80.142.93 \
  --google-account cuenta@gmail.com
```

El script:

- crea `/opt/autotube-egress/<slug>/` con permisos 700;
- genera el token del agente y el password VNC;
- escribe `agent_config.json` y `vnc.pass`;
- crea y arranca `egress-<slug>.service` (Docker + systemd);
- imprime la entrada para `config/egress_agents.json` y el comando de túnel VNC.

## Verificar

```sh
curl -s http://IP_TAILNET:9106/ping
curl -s http://IP_TAILNET:9106/healthz
```

En el servidor principal:

```sh
python3 scripts/verify_egress.py --slug canal6 --skip-browser
python3 scripts/verify_egress.py --slug canal6   # incluye navegador + WebRTC
```

## VNC

```sh
ssh -L 5956:127.0.0.1:5956 root@IP_TAILNET
```

TightVNC Viewer → `localhost:5956` con el password que imprimió el instalador.
Dentro del escritorio, lanzar siempre:

```sh
/app/deploy/egress-node/navegador.sh
```

Ese lanzador usa el perfil del canal y la política anti-fuga WebRTC.

## Actualizar

```sh
git pull --ff-only
IMAGE=autotube-egress:1 ./deploy/egress-node/build.sh
systemctl restart egress-canal6.service
```

## Operación

- `/healthz` devuelve IP de egress, IP esperada, versión de staging y disco.
- El staging se purga por TTL (`staging_ttl_hours`) y tras `/cleanup`.
- El disco debe mantener >= 8 GB libres; el instalador avisa si no.
- No usar el navegador manual y las acciones Playwright del agente a la vez
  (comparten perfil). Si necesitas mantenimiento, para el canal en el panel.

## Troubleshooting

| Síntoma | Causa | Acción |
|---|---|---|
| `healthz` con `egress_ok=false` | IP de salida distinta de `expected_ip` | Revisar red/proxy del nodo; no operar el canal |
| `/stage` 507 | Disco bajo el mínimo | Liberar espacio o ampliar volumen |
| `/stage` 413 | Cuota de staging o `max_body_mb` | Revisar `/cleanup` y tamaño del mp4 |
| VNC no conecta | Túnel mal o servicio caído | `systemctl status egress-<slug>` y logs del contenedor |
| Chromium no arranca | Falta imagen/deps | Reconstruir la imagen y reiniciar |
