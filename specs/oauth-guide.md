# Guía OAuth — Cómo autenticar un nuevo canal (sin perder 1h)

## Lo que NO funciona
- `run_local_server()` en server headless → no hay navegador
- `run_console()` → NO EXISTE en `google-auth-oauthlib >= 1.0`
- OAuth Playground con redirect_uri `http://localhost` → `redirect_uri_mismatch`

## Lo que SÍ funciona (5 min)

### Arquitectura
```
1 app OAuth en Cloud Console (1 client_secret.json)
  └── N cuentas Google autorizan → N tokens pickle por canal
```

Cada canal tiene su propio `client_secret_{slug}.json` (priority) o usa el default.

### Paso a paso (para un nuevo canal)

#### A. Cloud Console (una vez por app OAuth)
1. Ir a https://console.cloud.google.com/apis/credentials
2. Crear credencial OAuth → Aplicación de escritorio
3. Pantalla de consentimiento → **Externo** → añadir email como tester
4. Scopes: `youtube` + `yt-analytics.readonly`
5. Descargar JSON → guardar como `config/client_secret_{slug}.json`

#### B. Generar token (desde máquina con navegador — oficina, casa)
```bash
# 1. Copiar el script desde el server
# (o descargar specs/oauth_quick.py y editar CLIENT_SECRET_JSON)

# 2. Instalar dependencia
pip3 install google-auth-oauthlib

# 3. Ejecutar
python3 oauth_quick.py

# 4. Se abre navegador → autorizar con la cuenta Google del canal
# 5. Copiar el REFRESH TOKEN que sale en consola
```

#### C. Guardar token en el server
```bash
# Desde el server, con el refresh token:
python3 -c "
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

creds = Credentials(
    token=None,
    refresh_token='EL_REFRESH_TOKEN',
    token_uri='https://oauth2.googleapis.com/token',
    client_id='TU_CLIENT_ID',
    client_secret='TU_CLIENT_SECRET',
    scopes=['https://www.googleapis.com/auth/youtube',
            'https://www.googleapis.com/auth/yt-analytics.readonly'],
)
creds.refresh(Request())

with open('tokens/{slug}.pickle', 'wb') as f:
    pickle.dump(creds, f)

# Verificar
youtube = build('youtube', 'v3', credentials=creds)
print(youtube.channels().list(part='snippet', mine=True).execute())
"
```

#### D. Alternativa: desde el panel web
1. Panel → Canal → "Conectar YouTube"
2. Abrir URL en navegador
3. Autorizar → copiar código de la barra de direcciones
4. Pegar código en el modal → completar

---

## Ejemplo: canal2 (Sincronías)
- App: `youtube-uploads-automation` (client_id `415608242228-...`)
- Cuenta Google: `tracatrack@gmail.com`
- Token: `tokens/canal2.pickle`
- Generado desde: `oficina` (100.117.92.74) vía `oauth_quick.py`
- Canal YouTube: `@cleanthelistemaillistclean7103`

## Script listo para usar
Ver `scripts/oauth_quick.py` — copiar a máquina con navegador, editar CLIENT_SECRET_JSON, ejecutar.
