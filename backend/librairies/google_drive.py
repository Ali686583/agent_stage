"""
librairies/google_drive.py
============================

Integration Google Drive pour le bouton "Resume Drive" (banque de boutons).

Google Drive n'expose pas les documents PRIVES d'un utilisateur via une
simple clef API (contrairement a la banque de connexions generique, voir
librairies/platform_client.py) : il faut OAuth 2.0 (l'utilisateur autorise
explicitement cette application a lire SES documents). C'est pourquoi cette
integration est separee de connections_bank -- un jeton OAuth est personnel
a un utilisateur de CETTE application, jamais une clef partageable.

GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI
sont fournies par l'environnement Railway (jamais codees en dur, jamais
journalisees). Sans elles, ce module reste important sans erreur (les routes
qui l'utilisent repondent alors explicitement "non configure", jamais une
fausse reussite -- voir GoogleDriveConfigError ci-dessous).
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import requests

from librairies import database
from librairies.crypto_secrets import decrypt_secret, encrypt_secret

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")

OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
# Lecture seule : cette integration ne modifie jamais un document, elle en
# lit seulement le contenu pour le resumer.
OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Taille maximale de texte extrait avant de refuser (mission §5 : "document
# trop volumineux" doit etre gere proprement, jamais tronque en silence puis
# presente comme le document complet). ~300k caracteres est deja largement
# au-dela de ce qu'un prompt raisonnable peut exploiter utilement.
MAX_DOCUMENT_CHARS = 300_000


class GoogleDriveConfigError(RuntimeError):
    """Levee quand les identifiants OAuth Google ne sont pas configures."""


class GoogleDriveError(RuntimeError):
    """Erreur fonctionnelle (document introuvable, prive, trop volumineux,
    type non supporte, etc.). Le message est un CODE court et stable
    (jamais un texte libre) : voir jobs.py et workspace_routes.py, qui le
    traduisent pour l'utilisateur -- jamais une trace brute affichee."""


def _require_config() -> None:
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET or not GOOGLE_OAUTH_REDIRECT_URI:
        raise GoogleDriveConfigError(
            "GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET/GOOGLE_OAUTH_REDIRECT_URI "
            "ne sont pas configurees : voir librairies/google_drive.py."
        )


def is_configured() -> bool:
    return bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI)


def build_authorization_url(state: str) -> str:
    """Construit l'URL de consentement Google vers laquelle rediriger le
    navigateur. `state` doit etre une valeur imprevisible liee a la session
    en cours (protection CSRF standard du flux OAuth), fournie par l'appelant
    (voir workspace_routes.py)."""
    _require_config()
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "access_type": "offline",
        # "consent" force Google a renvoyer un refresh_token a CHAQUE
        # autorisation (par defaut, Google ne le renvoie qu'une seule fois
        # au tout premier consentement) : necessaire pour pouvoir se
        # reconnecter proprement apres une deconnexion.
        "prompt": "consent",
        "state": state,
    }
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"{OAUTH_AUTH_URL}?{query}"


def exchange_code_for_tokens(code: str) -> dict:
    """Echange le code d'autorisation contre un access_token + refresh_token.
    Leve GoogleDriveError si Google refuse (code invalide/expire)."""
    _require_config()
    response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not response.ok:
        raise GoogleDriveError("oauth_exchange_failed")
    return response.json()


def _fetch_google_email(access_token: str) -> str | None:
    """Best-effort : n'est jamais indispensable au fonctionnement du bouton,
    seulement affiche a l'utilisateur pour qu'il sache QUEL compte Google est
    connecte (voir GET /integrations/google-drive/status)."""
    try:
        response = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.ok:
            return response.json().get("email")
    except requests.exceptions.RequestException:
        pass
    return None


def connect_user(user_id: str, code: str) -> None:
    """Termine le flux OAuth pour cet utilisateur : echange le code, chiffre
    et persiste le refresh_token. Leve GoogleDriveError si Google ne renvoie
    pas de refresh_token (arrive si l'utilisateur avait deja consenti sans
    prompt=consent -- ne devrait pas arriver ici, voir build_authorization_url,
    mais on ne suppose jamais silencieusement une valeur absente)."""
    tokens = exchange_code_for_tokens(code)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise GoogleDriveError("no_refresh_token")
    google_email = _fetch_google_email(tokens.get("access_token", "")) if tokens.get("access_token") else None
    database.set_google_drive_credential(user_id, encrypt_secret(refresh_token), google_email)


def disconnect_user(user_id: str) -> bool:
    return database.delete_google_drive_credential(user_id)


def get_connection_status(user_id: str) -> dict:
    credential = database.get_google_drive_credential(user_id)
    return {"connected": bool(credential), "googleEmail": credential["googleEmail"] if credential else None}


def get_valid_access_token(user_id: str) -> str | None:
    """Renvoie un access_token frais (toujours redemande au refresh_token
    stocke plutot que mis en cache : les appels a ce bouton restent rares,
    la simplicite l'emporte sur l'economie d'un aller-retour reseau), ou
    None si l'utilisateur n'a jamais connecte son compte Google Drive."""
    credential = database.get_google_drive_credential(user_id)
    if not credential:
        return None
    _require_config()
    refresh_token = decrypt_secret(credential["refreshTokenEncrypted"])
    response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if not response.ok:
        # Refresh token revoque/expire cote Google (l'utilisateur a retire
        # l'acces depuis son compte Google, par exemple) : jamais une
        # exception opaque, l'appelant doit pouvoir dire clairement
        # "reconnecte ton compte Google Drive".
        return None
    return response.json().get("access_token")


# Formats Google natifs (Docs/Sheets/Slides) : n'existent que dans Drive, pas
# de fichier binaire telechargeable directement -- il faut les "exporter"
# vers un format texte via l'API Drive.
_EXPORT_MIME_BY_GOOGLE_TYPE = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Fichiers UPLOADES (pas natifs Google) dont le contenu est directement du
# texte lisible : pris en charge sans bibliotheque d'extraction specifique.
# LIMITE HONNETE (a signaler, pas a masquer) : un PDF/Word/Excel uploade tel
# quel dans Drive n'est PAS pris en charge ici -- son contenu est binaire et
# extraire son texte demanderait une bibliotheque dediee (non presente dans
# requirements.txt, non ajoutee ici pour ne pas construire une extraction
# non testee). Renvoie alors "unsupported_file_type" plutot que d'inventer
# un contenu ou une extraction partielle non fiable.
_DIRECT_TEXT_MIME_TYPES = {"text/plain", "text/csv", "text/markdown"}


def extract_drive_file_id(text: str) -> str | None:
    """Retrouve un identifiant de document Google Drive dans un texte libre :
    accepte un lien Drive/Docs/Sheets/Slides complet ou un identifiant brut
    deja isole. Ne devine jamais un identifiant partiel."""
    if not text:
        return None
    cleaned = text.strip()
    match = re.search(r"/d/([a-zA-Z0-9_-]{10,})", cleaned)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]{10,})", cleaned)
    if match:
        return match.group(1)
    # Dernier recours : le texte entier ressemble deja a un identifiant brut
    # (aucun espace, alphabet Drive uniquement) -- jamais si le message
    # contient aussi une phrase autour, pour ne jamais deviner a tort.
    if re.fullmatch(r"[a-zA-Z0-9_-]{15,}", cleaned):
        return cleaned
    return None


def fetch_document_text(access_token: str, file_id: str) -> str:
    """Recupere le contenu textuel d'un document Drive. Leve GoogleDriveError
    avec un code stable pour chaque cas d'echec (mission §5) ; n'invente
    jamais de contenu si la recuperation echoue."""
    headers = {"Authorization": f"Bearer {access_token}"}
    meta_response = requests.get(
        f"{DRIVE_API_URL}/files/{file_id}",
        headers=headers,
        params={"fields": "id,name,mimeType,size"},
        timeout=15,
    )
    if meta_response.status_code == 404:
        raise GoogleDriveError("document_not_found")
    if meta_response.status_code in (401, 403):
        raise GoogleDriveError("permission_denied")
    if not meta_response.ok:
        raise GoogleDriveError("drive_api_error")
    metadata = meta_response.json()
    mime_type = metadata.get("mimeType", "")
    size = metadata.get("size")
    if size is not None and int(size) > MAX_DOCUMENT_CHARS * 4:
        # Heuristique large (4 octets/caractere max) : evite de telecharger
        # inutilement un fichier deja hors limite d'apres sa taille brute.
        raise GoogleDriveError("document_too_large")

    if mime_type in _EXPORT_MIME_BY_GOOGLE_TYPE:
        export_mime = _EXPORT_MIME_BY_GOOGLE_TYPE[mime_type]
        content_response = requests.get(
            f"{DRIVE_API_URL}/files/{file_id}/export",
            headers=headers,
            params={"mimeType": export_mime},
            timeout=30,
        )
    elif mime_type in _DIRECT_TEXT_MIME_TYPES:
        content_response = requests.get(
            f"{DRIVE_API_URL}/files/{file_id}", headers=headers, params={"alt": "media"}, timeout=30
        )
    else:
        raise GoogleDriveError("unsupported_file_type")

    if content_response.status_code in (401, 403):
        raise GoogleDriveError("permission_denied")
    if not content_response.ok:
        raise GoogleDriveError("drive_api_error")

    text = content_response.text or ""
    if not text.strip():
        raise GoogleDriveError("empty_document")
    if len(text) > MAX_DOCUMENT_CHARS:
        raise GoogleDriveError("document_too_large")
    return text
