"""
Obtiene un refresh token de Google con permiso de solo lectura sobre las estadísticas de Postmaster Tools (API v2).

Uso (en TU computadora, no en el servidor):
  1. pip install google-auth-oauthlib
  2. Descargá el JSON del cliente OAuth (tipo "App de escritorio") desde Google Cloud
     y guardalo junto a este script como client_secret.json
  3. python get_postmaster_token.py
  4. Se abre el navegador: iniciá sesión con la cuenta de Google que ve
     los dominios en Postmaster Tools y aceptá el permiso.
  5. El script imprime las 3 variables que hay que cargar en el entorno de la tarea.

No compartas la salida por chat ni la subas a un repo: el refresh token da acceso
de lectura a tus datos de Postmaster hasta que lo revoques.
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/postmaster.traffic.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

with open("client_secret.json") as f:
    data = json.load(f)
client = data.get("installed") or data["web"]

if not creds.refresh_token:
    raise SystemExit(
        "Google no devolvió refresh token. Revocá el acceso de la app en "
        "myaccount.google.com/permissions y volvé a correr el script."
    )

print("\nCargá estas variables en el entorno de la tarea programada:\n")
print(f"GOOGLE_CLIENT_ID={client['client_id']}")
print(f"GOOGLE_CLIENT_SECRET={client['client_secret']}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
