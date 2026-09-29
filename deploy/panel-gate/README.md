# panel-gate — login de patrón universal (snapshot versionado)

`panel-gate` es el **reverse-proxy con desbloqueo por patrón 3×3** (estilo Android)
que protege los paneles privados servidos bajo `josue.ink`. Vive en el servidor en
`/root/panel-gate` (fuera del repo autotube); este directorio es un **snapshot
versionado** para tener el código y su configuración bajo control de cambios.

> ⚠️ **Drift:** este snapshot es una copia de referencia. La copia viva es
> `/root/panel-gate`. Al cambiar el gate, actualiza ambos (o re-sincroniza con el
> comando de más abajo) para que no divergan.

## Cómo funciona

| Pieza | Ruta | Función |
|---|---|---|
| Servidor | `server.js` | Express + http-proxy-middleware; enruta por `Host`/path e inyecta `X-Panel-Gate: 1` |
| Patrón | `lib/pattern.js` | scrypt + `timingSafeEqual` (nunca se guarda el patrón en claro) |
| Sesión | `lib/session.js` | token HMAC-SHA256 (`pg_session`, TTL 30 d) + lista de revocación |
| `.env` | `lib/env.js` | parser/actualizador de `.env` (relee `PATTERN_HASH` en cada intento) |
| UI | `public/unlock.html`, `public/pattern.js`, `public/style.css` | pantalla de patrón (Pointer Events, canvas) |
| CLI | `bin/set-pattern.js`, `bin/hash-secret.js` | fijar patrón / regenerar secreto |
| systemd | `../systemd/panel-gate.service` | unidad de servicio (`/root/panel-gate`) |

Enrutado por `Host` + path:

| Backend | Destino | Criterio |
|---|---|---|
| `BACKEND_CERRADURAS` | `127.0.0.1:8080` | host `cerraduras.*` |
| `BACKEND_PANELS` | `127.0.0.1:8085` | resto (admin, trading, reconocimientoFacial) |
| `BACKEND_TAILDECK` | `127.0.0.1:8110` | path `/superserver` |
| `BACKEND_AUTOTUBE` | `127.0.0.1:8000` | path `/autotube`, `/api`, `/ws` |

Para autotube se elimina el prefijo `/autotube` al proxear
(`/autotube/assets/x.js` → `/assets/x.js`); `/api` y `/ws` pasan intactos.

## Variables de entorno (`/root/panel-gate/.env`, chmod 600, NO versionado)

```dotenv
PORT=8099
GATE_SECRET=<64 hex; node bin/hash-secret.js>
PATTERN_HASH=<scrypt...; node bin/set-pattern.js "0-1-2-5-8-3">
SESSION_TTL_DAYS=30
COOKIE_DOMAIN=.josue.ink
COOKIE_SECURE=true
BACKEND_CERRADURAS=http://127.0.0.1:8080
BACKEND_PANELS=http://127.0.0.1:8085
BACKEND_TAILDECK=http://127.0.0.1:8110
BACKEND_AUTOTUBE=http://127.0.0.1:8000
FAIL_MAX=5
LOCK_MIN=15
PATTERN_MIN=4
```

## Instalación / actualización

```sh
# 1) Sincronizar la copia viva (solo si has cambiado el snapshot)
cp -a deploy/panel-gate/server.js deploy/panel-gate/lib deploy/panel-gate/public \
      deploy/panel-gate/bin /root/panel-gate/
node --check /root/panel-gate/server.js

# 2) Dependencias (primera vez o si cambia package.json)
cd /root/panel-gate && npm ci --omit=dev

# 3) Backend de autotube y resto de backends en /root/panel-gate/.env
#    (fichero chmod 600, NO versionado)
#    BACKEND_AUTOTUBE=http://127.0.0.1:8000

# 4) Reiniciar el gate
systemctl restart panel-gate && systemctl is-active panel-gate
```

## Definir / cambiar el patrón

```sh
node /root/panel-gate/bin/set-pattern.js "0-1-2-5-8-3"   # >= PATTERN_MIN puntos, 0..8, sin repetir
```

El patrón es **universal**: el mismo desbloqueo vale para `/admin`, `/trading`,
`/reconocimientoFacial`, `/superserver` y `/autotube` (cookie `pg_session` con
dominio `.josue.ink`). El servidor relee `PATTERN_HASH` en cada intento, así que
el cambio aplica al instante (no hace falta reiniciar).

Para invalidar todas las sesiones: `node /root/panel-gate/bin/hash-secret.js` y
`systemctl restart panel-gate`.

## Apache

El snippet que integra autotube en los vhosts de `josue.ink` está en
`../apache/josue-autotube-gate.conf`.

```sh
apache2ctl configtest && systemctl reload apache2
```

## Verificación

```sh
# Sin sesión -> pantalla de patrón
curl -sk -H 'Host: josue.ink' https://127.0.0.1/autotube/ | grep -c 'Acceso restringido'

# Con sesión válida (token firmado) -> SPA/API/media 200
TOKEN=$(node -e "const s=require('/root/panel-gate/lib/session');require('/root/panel-gate/lib/env').loadEnv();process.stdout.write(s.issue(process.env.GATE_SECRET,30).token)")
curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: josue.ink' -H "Cookie: pg_session=$TOKEN" https://127.0.0.1/autotube/
curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: josue.ink' -H "Cookie: pg_session=$TOKEN" https://127.0.0.1/api/channels
```

## Logs

`/root/panel-gate/logs/auth.log` (eventos de desbloqueo/fallo) y `access.log`.
