# ADR-20261109 — Egress aislado por canal con gateway contenedor

## Estado
Aceptado. Implementado para los canales nuevos; los 4 existentes no se migran.

## Contexto
Los 4 canales actuales comparten la IP del servidor principal. Un strike/ban de
uno puede vincular a los demás por IP. Se van a crear 2 canales nuevos y se
quiere que cada uno tenga una identidad de salida independiente, con la misma
cuenta Google/GCP/perfil solo para él.

## Decisión
Cada canal nuevo tiene un **gateway egress persistente** en un contenedor
(`deploy/egress-node/`): agente HTTP + Chromium/Playwright + XFCE/X11VNC. El
contenedor sale por la IP del nodo (o por un proxy por canal en el futuro) y
guarda en `/data` el OAuth, `client_secret`, perfil y staging. No monta la DB
del servidor principal (`AUTOTUBE_AGENT_MODE=1`).

El servidor principal nunca contacta YouTube/Google para un canal gestionado:

- `channels.egress_managed` es la marca durable (DB).
- `api/services/egress_registry.py` resuelve el perfil; si falta `url`, `token`
  o `expected_ip` lanza `EgressConfigurationError` (fail-closed).
- `api/services/youtube_gateway.py` es la fachada única.
- `api/services/egress_guard.py` bloquea en runtime DNS directo a
  Google/YouTube desde el worker de un canal gestionado.
- `scripts/audit_egress_coverage.py` impide que aparezcan rutas locales nuevas.

## Consecuencias
- Un gateway caído bloquea el canal; no hay fallback a la IP del servidor.
- El staging se borra tras `/cleanup`, por TTL y con cuotas de disco.
- La subida es idempotente por `upload_ref` (ledger en el gateway).
- El alta de cuenta/GCP/OAuth sigue siendo manual (asistida por VNC); el
  software prepara el gateway, valida la IP, despliega el `client_secret` y
  verifica el egress.
- Los 2 canales nuevos comparten la IP del nodo; quedan aislados de los 4
  actuales, pero no entre sí. El diseño admite un proxy por canal para separarlos
  más adelante.
- Un host antiguo (Debian 8) es un riesgo; el contenedor aísla el software, pero
  se recomienda un VPS actualizado para producción seria.

## Alternativas descartadas
- Ejecutar la subida como job efímero de SuperServer: las credenciales y el
  perfil no sobreviven al teardown; se descarta.
- Compartir una única IP nueva para todos los canales: reproduce el riesgo
  actual.
- Automatizar la creación de cuentas Google: riesgo alto de suspensión.
