"""
librairies/connections_bank.py
================================

Banque de connexions plateformes/API (nouveau bouton "Plateformes/API" de
l'entry, place immediatement a gauche du trombone). Meme base PostgreSQL
SEPAREE que la banque de boutons (Postgres-jg_R, voir
librairies/workflow_bank.py) : "Postgres-jg_R pour la banque de workflow,
Postgres pour tout le reste".

Architecture volontairement calquee sur workflow_bank.py : une banque
CENTRALE et PARTAGEE (comme les boutons), pas une bibliotheque par
utilisateur -- n'importe quel utilisateur authentifie de cet espace
collaboratif peut retrouver, selectionner et utiliser une connexion deja
enregistree par quelqu'un d'autre (cf. le prompt : "Boutons partages" §8,
applique ici a l'identique aux connexions).

RÈGLE ABSOLUE (prompt §27-29) : la clef API en clair ne quitte JAMAIS ce
module. Elle est chiffree (librairies/crypto_secrets.py) avant d'etre
ecrite en base, et aucune fonction de ce fichier autre que
get_connection_secret() (cle API) / get_oauth_client_config() /
get_oauth_tokens() (OAuth) ne la dechiffre. Toute fonction qui peut etre
appelee (directement ou indirectement) depuis une route HTTP renvoie
uniquement _public_connection(), qui ne contient jamais de secret en clair.

TROIS ENTREES INDEPENDANTES ET FACULTATIVES (chacune peut etre vide, remplie,
ou combinee avec les autres) : une connexion "toutes plateformes" doit
couvrir une API a cle simple (entree 1 : Bearer/en-tete personnalise/
parametre d'URL), une plateforme exigeant OAuth2 (entree 2 : l'utilisateur
fournit sa propre app OAuth -- client_id/secret + URLs d'autorisation/de
jeton -- obtenue sur la console developpeur de la plateforme visee, voir
librairies/oauth_connector.py qui gere le flux reel), et un serveur MCP
(entree 3 : URL + authentification facultative). Contrairement a
librairies/google_drive.py (jeton personnel par UTILISATEUR), un jeton
OAuth ici est stocke au niveau de la CONNEXION et partage par tout l'espace
collaboratif, coherent avec le reste de cette banque.

ENTREE 3 (serveur MCP) -- DIFFERENCE D'ARCHITECTURE IMPORTANTE avec les
entrees 1/2 : pour une cle API ou un jeton OAuth, c'est CE backend qui
appelle la plateforme externe (voir librairies/platform_client.py et
librairies/oauth_connector.py) et ne transmet a n8n QUE le resultat deja
recupere. Un serveur MCP, lui, est appele DIRECTEMENT par n8n (bloc "AI
Agent" + "MCP Client", qui decident eux-memes, pendant la generation de la
reponse, quel outil appeler) -- ce backend ne fait jamais cet appel
lui-meme. Consequence assumee et documentee : le secret MCP (s'il y en a
un) est donc dechiffre puis transmis a n8n dans la charge utile de chaque
requete (voir get_mcp_config_with_secret ci-dessous et librairies/jobs.py),
jamais journalise, jamais renvoye via une route HTTP.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from librairies.crypto_secrets import decrypt_secret, encrypt_secret

BANK_DATABASE_URL = os.environ.get("WORKFLOW_BANK_DATABASE_URL", "")

MAX_KEYWORDS = 15
MAX_KEYWORD_LENGTH = 40

AUTH_LOCATIONS = ("header_bearer", "header_custom", "query_param")
# Entree 3 (serveur MCP) : options plus restreintes que AUTH_LOCATIONS car
# c'est n8n (pas ce backend) qui authentifie l'appel -- "none" est une
# option a part entiere ici (beaucoup de serveurs MCP de test/internes n'en
# ont pas besoin), et "query_param" n'a pas de sens pour un serveur MCP.
MCP_AUTH_LOCATIONS = ("none", "header_bearer", "header_custom")
# Seul le transport SSE est verifie fonctionnel sur l'instance n8n de ce
# projet (le noeud MCP Client de la version installee n'expose qu'un champ
# "sseEndpoint", confirme en testant reellement -- voir le rapport fourni a
# l'utilisateur). Champ conserve pour permettre une evolution future sans
# migration si n8n ajoute un jour un autre transport, mais une seule valeur
# est acceptee pour l'instant : ne jamais laisser croire qu'un autre
# transport est pris en charge alors qu'il ne l'est pas.
MCP_TRANSPORTS = ("sse",)


@contextmanager
def _db():
    if not BANK_DATABASE_URL:
        raise RuntimeError(
            "WORKFLOW_BANK_DATABASE_URL n'est pas configuree : voir librairies/connections_bank.py."
        )
    conn = psycopg.connect(BANK_DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_connections_db() -> None:
    """Cree la table si elle n'existe pas encore. A appeler au demarrage.

    Meme verrou consultatif que workflow_bank.init_bank_db()/database.init_db()
    (cle differente : 727001727003) pour la meme raison exacte : eviter la
    UniqueViolation deja observee en production quand plusieurs workers
    gunicorn creent les tables en meme temps au tout premier demarrage."""
    with _db() as conn:
        conn.execute("SELECT pg_advisory_lock(727001727003);")
        _create_tables(conn)
        conn.execute("SELECT pg_advisory_unlock(727001727003);")


def _create_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS connections (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            platform_type    TEXT,
            keywords         TEXT[] NOT NULL DEFAULT '{}',
            config           JSONB NOT NULL DEFAULT '{}'::jsonb,
            secret_encrypted TEXT,
            created_by       TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_connections_status ON connections (status);")
    # Migration additive (deja deploye en production avec ces deux colonnes
    # NOT NULL, du temps ou "Plateforme/Type" et la cle API etaient
    # obligatoires) : desormais les deux entrees du formulaire sont
    # facultatives, donc ces colonnes doivent pouvoir etre NULL.
    conn.execute("ALTER TABLE connections ALTER COLUMN platform_type DROP NOT NULL;")
    conn.execute("ALTER TABLE connections ALTER COLUMN secret_encrypted DROP NOT NULL;")
    # Secret de la 2e entree possible (config OAuth generique) : independant
    # de secret_encrypted (cle API de la 1re entree), voir le docstring du
    # module.
    conn.execute("ALTER TABLE connections ADD COLUMN IF NOT EXISTS oauth_client_secret_encrypted TEXT;")
    # Secret de la 3e entree possible (authentification du serveur MCP,
    # entree 3) : independant des deux precedents, voir le docstring du
    # module.
    conn.execute("ALTER TABLE connections ADD COLUMN IF NOT EXISTS mcp_secret_encrypted TEXT;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS connection_oauth_tokens (
            connection_id          TEXT PRIMARY KEY REFERENCES connections (id) ON DELETE CASCADE,
            access_token_encrypted  TEXT,
            refresh_token_encrypted TEXT,
            expires_at              TIMESTAMPTZ,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


# ---------------------------------------------------------------------------
# Serialisation -- ne contient JAMAIS secret_encrypted ni la clef en clair.
# ---------------------------------------------------------------------------

def _public_connection(row) -> dict:
    config = row["config"] or {}
    has_oauth_config = bool(
        config.get("oauthClientId") and config.get("oauthAuthorizeUrl") and config.get("oauthTokenUrl") and row.get("oauth_client_secret_encrypted")
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "platformType": row["platform_type"] or "",
        "keywords": list(row["keywords"] or []),
        # Entree 1 (cle API generalisee) : jamais la cle en clair, juste de
        # quoi savoir si elle est renseignee et comment elle est envoyee.
        "hasApiKey": bool(row["secret_encrypted"]),
        "hasBaseUrl": bool(config.get("baseUrl")),
        "authLocation": config.get("authLocation") or "header_bearer",
        "authFieldName": config.get("authFieldName") or "",
        # Entree 2 (OAuth generique) : configuree (app OAuth fournie) et/ou
        # deja connectee (jeton obtenu) sont deux etats distincts.
        "hasOAuthConfig": has_oauth_config,
        "oauthConnected": bool(row.get("oauth_connected")),
        # Entree 3 (serveur MCP) : voir le docstring du module -- appelee
        # directement par n8n, jamais par ce backend.
        "hasMcpConfig": bool(config.get("mcpServerUrl")),
        "mcpTransport": config.get("mcpTransport") or "sse",
        "mcpAuthLocation": config.get("mcpAuthLocation") or "none",
        "mcpAuthFieldName": config.get("mcpAuthFieldName") or "",
        "createdBy": row["created_by"],
        "connected": True,
        "createdAt": row["created_at"].isoformat(),
    }


def _internal_connection(row) -> dict:
    """Usage strictement interne (librairies/platform_client.py) : contient
    `config` (dont l'URL de base) en plus des champs publics. Jamais expose
    via une route -- voir get_connection_secret, seule fonction qui
    construit ce dict."""
    public = _public_connection(row)
    public["config"] = dict(row["config"] or {})
    return public


def _clean_keywords(keywords) -> list[str]:
    if not isinstance(keywords, list):
        return []
    cleaned = []
    for kw in keywords:
        value = str(kw or "").strip()[:MAX_KEYWORD_LENGTH]
        if value and value.lower() not in [c.lower() for c in cleaned]:
            cleaned.append(value)
        if len(cleaned) >= MAX_KEYWORDS:
            break
    return cleaned


def _clean_config(config: dict | None) -> dict:
    """N'accepte que des cles connues (jamais une configuration arbitraire
    fournie par le frontend) : voir librairies/platform_client.py (entree 1,
    cle API), librairies/oauth_connector.py (entree 2, OAuth) et
    librairies/jobs.py (entree 3, MCP -- transmise telle quelle a n8n) pour
    leur usage."""
    config = config or {}
    cleaned: dict = {}
    base_url = str(config.get("baseUrl") or "").strip()[:500]
    if base_url.startswith("http://") or base_url.startswith("https://"):
        cleaned["baseUrl"] = base_url
    auth_location = str(config.get("authLocation") or "").strip()
    cleaned["authLocation"] = auth_location if auth_location in AUTH_LOCATIONS else "header_bearer"
    auth_field_name = str(config.get("authFieldName") or "").strip()[:100]
    if auth_field_name:
        cleaned["authFieldName"] = auth_field_name

    oauth_client_id = str(config.get("oauthClientId") or "").strip()[:200]
    if oauth_client_id:
        cleaned["oauthClientId"] = oauth_client_id
    oauth_authorize_url = str(config.get("oauthAuthorizeUrl") or "").strip()[:500]
    if oauth_authorize_url.startswith("http://") or oauth_authorize_url.startswith("https://"):
        cleaned["oauthAuthorizeUrl"] = oauth_authorize_url
    oauth_token_url = str(config.get("oauthTokenUrl") or "").strip()[:500]
    if oauth_token_url.startswith("http://") or oauth_token_url.startswith("https://"):
        cleaned["oauthTokenUrl"] = oauth_token_url
    oauth_scope = str(config.get("oauthScope") or "").strip()[:300]
    if oauth_scope:
        cleaned["oauthScope"] = oauth_scope

    mcp_server_url = str(config.get("mcpServerUrl") or "").strip()[:500]
    if mcp_server_url.startswith("http://") or mcp_server_url.startswith("https://"):
        cleaned["mcpServerUrl"] = mcp_server_url
    mcp_transport = str(config.get("mcpTransport") or "").strip()
    cleaned["mcpTransport"] = mcp_transport if mcp_transport in MCP_TRANSPORTS else "sse"
    mcp_auth_location = str(config.get("mcpAuthLocation") or "").strip()
    cleaned["mcpAuthLocation"] = mcp_auth_location if mcp_auth_location in MCP_AUTH_LOCATIONS else "none"
    mcp_auth_field_name = str(config.get("mcpAuthFieldName") or "").strip()[:100]
    if mcp_auth_field_name:
        cleaned["mcpAuthFieldName"] = mcp_auth_field_name
    return cleaned


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

# Jointure commune : ajoute oauth_connected (bool) a chaque ligne sans
# jamais exposer les jetons eux-memes (voir _public_connection).
_SELECT_WITH_OAUTH_STATE = """
    SELECT c.*, (t.connection_id IS NOT NULL) AS oauth_connected
    FROM connections c
    LEFT JOIN connection_oauth_tokens t ON t.connection_id = c.id
"""


def create_connection(
    name: str,
    created_by: str,
    keywords: list | None = None,
    platform_type: str = "",
    api_key: str = "",
    oauth_client_secret: str = "",
    mcp_secret: str = "",
    config: dict | None = None,
) -> dict:
    """Les trois entrees (cle API / OAuth / MCP) sont facultatives et
    independantes (voir le docstring du module) : api_key, oauth_client_secret
    et mcp_secret peuvent chacune etre vides, remplies, ou combinees. Jamais
    appele encrypt_secret() sur une valeur vide (voir crypto_secrets.py)."""
    connection_id = str(uuid.uuid4())
    secret_encrypted = encrypt_secret(api_key) if api_key else None
    oauth_client_secret_encrypted = encrypt_secret(oauth_client_secret) if oauth_client_secret else None
    mcp_secret_encrypted = encrypt_secret(mcp_secret) if mcp_secret else None
    with _db() as conn:
        row = conn.execute(
            """
            INSERT INTO connections (
                id, name, platform_type, keywords, config,
                secret_encrypted, oauth_client_secret_encrypted, mcp_secret_encrypted, created_by
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            RETURNING *;
            """,
            (
                connection_id,
                name,
                platform_type or None,
                _clean_keywords(keywords),
                Jsonb(_clean_config(config)),
                secret_encrypted,
                oauth_client_secret_encrypted,
                mcp_secret_encrypted,
                created_by,
            ),
        ).fetchone()
        row["oauth_connected"] = False
        return _public_connection(row)


def list_connections(search: str = "", limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            _SELECT_WITH_OAUTH_STATE + " WHERE c.status = 'active' AND c.name ILIKE %s ORDER BY c.name ASC LIMIT %s;",
            (f"%{search}%", limit),
        ).fetchall()
        return [_public_connection(row) for row in rows]


def list_connections_by_ids(connection_ids: list[str]) -> list[dict]:
    if not connection_ids:
        return []
    with _db() as conn:
        rows = conn.execute(
            _SELECT_WITH_OAUTH_STATE + " WHERE c.id = ANY(%s) AND c.status = 'active';",
            (connection_ids,),
        ).fetchall()
        return [_public_connection(row) for row in rows]


def get_connection(connection_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            _SELECT_WITH_OAUTH_STATE + " WHERE c.id = %s AND c.status = 'active';", (connection_id,)
        ).fetchone()
        return _public_connection(row) if row else None


def get_connection_secret(connection_id: str) -> tuple[dict, str] | None:
    """Usage strictement interne (librairies/platform_client.py, appele
    depuis le worker) : renvoie (connexion_publique, clef_en_clair -- chaine
    vide si l'entree 1 n'a pas ete renseignee), ou None si introuvable. Ne
    JAMAIS exposer cette fonction via une route HTTP."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = %s AND status = 'active';", (connection_id,)
        ).fetchone()
        if not row:
            return None
        secret = decrypt_secret(row["secret_encrypted"]) if row["secret_encrypted"] else ""
        return _internal_connection(row), secret


# ---------------------------------------------------------------------------
# Entree 2 (OAuth generique) : voir librairies/oauth_connector.py, qui
# orchestre le flux HTTP reel (redirection, echange de code, rafraichissement)
# en s'appuyant uniquement sur ces accesseurs -- jamais de logique HTTP ici.
# ---------------------------------------------------------------------------

def get_oauth_client_config(connection_id: str) -> dict | None:
    """Usage strictement interne (librairies/oauth_connector.py) : renvoie la
    configuration OAuth dechiffree de cette connexion, ou None si la
    connexion n'existe pas OU si l'entree 2 n'a pas ete renseignee (au moins
    un champ requis manquant) -- jamais une config partielle silencieuse."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = %s AND status = 'active';", (connection_id,)
        ).fetchone()
    if not row:
        return None
    config = row["config"] or {}
    client_id = config.get("oauthClientId")
    authorize_url = config.get("oauthAuthorizeUrl")
    token_url = config.get("oauthTokenUrl")
    if not client_id or not authorize_url or not token_url or not row["oauth_client_secret_encrypted"]:
        return None
    return {
        "clientId": client_id,
        "clientSecret": decrypt_secret(row["oauth_client_secret_encrypted"]),
        "authorizeUrl": authorize_url,
        "tokenUrl": token_url,
        "scope": config.get("oauthScope") or "",
    }


def set_oauth_tokens(connection_id: str, access_token: str, refresh_token: str | None, expires_in: int | None) -> None:
    """Persiste (creation ou remplacement) le jeu de jetons OAuth d'une
    connexion, chiffres. Usage strictement interne
    (librairies/oauth_connector.py)."""
    access_token_encrypted = encrypt_secret(access_token) if access_token else None
    refresh_token_encrypted = encrypt_secret(refresh_token) if refresh_token else None
    expires_at = time.time() + float(expires_in) if expires_in else None
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO connection_oauth_tokens (connection_id, access_token_encrypted, refresh_token_encrypted, expires_at, updated_at)
            VALUES (%s, %s, %s, to_timestamp(%s), now())
            ON CONFLICT (connection_id) DO UPDATE SET
                access_token_encrypted = EXCLUDED.access_token_encrypted,
                refresh_token_encrypted = COALESCE(EXCLUDED.refresh_token_encrypted, connection_oauth_tokens.refresh_token_encrypted),
                expires_at = EXCLUDED.expires_at,
                updated_at = now();
            """,
            (connection_id, access_token_encrypted, refresh_token_encrypted, expires_at),
        )


def get_oauth_tokens(connection_id: str) -> dict | None:
    """Usage strictement interne (librairies/oauth_connector.py) : renvoie
    les jetons dechiffres de cette connexion, ou None si jamais connectee."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connection_oauth_tokens WHERE connection_id = %s;", (connection_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "accessToken": decrypt_secret(row["access_token_encrypted"]) if row["access_token_encrypted"] else "",
        "refreshToken": decrypt_secret(row["refresh_token_encrypted"]) if row["refresh_token_encrypted"] else "",
        "expiresAt": row["expires_at"].timestamp() if row["expires_at"] else None,
    }


def clear_oauth_tokens(connection_id: str) -> bool:
    """Deconnexion OAuth : supprime le jeton stocke, la connexion elle-meme
    (nom, cle API eventuelle, config) reste intacte -- symetrique de
    "Se connecter", qui ne fait que rajouter un jeton."""
    with _db() as conn:
        cursor = conn.execute("DELETE FROM connection_oauth_tokens WHERE connection_id = %s;", (connection_id,))
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Entree 3 (serveur MCP) : contrairement aux entrees 1/2, aucun appel HTTP
# n'est fait depuis ce backend -- voir librairies/jobs.py, qui transmet
# cette configuration (secret dechiffre inclus) a n8n, qui appelle lui-meme
# le serveur MCP via son bloc "AI Agent" + "MCP Client".
# ---------------------------------------------------------------------------

def get_mcp_config_with_secret(connection_id: str) -> dict | None:
    """Usage strictement interne (librairies/jobs.py) : renvoie la
    configuration MCP dechiffree de cette connexion, ou None si la connexion
    n'existe pas OU si l'entree 3 n'a pas ete renseignee (URL manquante).
    L'authentification est facultative (beaucoup de serveurs MCP internes/de
    test n'en ont pas besoin) : `secret` est une chaine vide dans ce cas,
    jamais une valeur inventee."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = %s AND status = 'active';", (connection_id,)
        ).fetchone()
    if not row:
        return None
    config = row["config"] or {}
    server_url = config.get("mcpServerUrl")
    if not server_url:
        return None
    return {
        "name": row["name"],
        "serverUrl": server_url,
        "transport": config.get("mcpTransport") or "sse",
        "authLocation": config.get("mcpAuthLocation") or "none",
        "authFieldName": config.get("mcpAuthFieldName") or "",
        "secret": decrypt_secret(row["mcp_secret_encrypted"]) if row["mcp_secret_encrypted"] else "",
    }


def rename_connection(connection_id: str, name: str, actor_user_id: str, is_admin: bool) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT created_by FROM connections WHERE id = %s;", (connection_id,)).fetchone()
        if not row:
            return None
        if row["created_by"] != actor_user_id and not is_admin:
            raise PermissionError("Seul le createur ou un administrateur peut renommer cette connexion.")
        updated = conn.execute(
            "UPDATE connections SET name = %s, updated_at = now() WHERE id = %s RETURNING *;",
            (name, connection_id),
        ).fetchone()
        updated["oauth_connected"] = conn.execute(
            "SELECT 1 FROM connection_oauth_tokens WHERE connection_id = %s;", (connection_id,)
        ).fetchone() is not None
        return _public_connection(updated)


def delete_connection(connection_id: str, actor_user_id: str, is_admin: bool) -> bool:
    """Suppression definitive. NOTE D'ARCHITECTURE (deviation assumee,
    signalee au lieu d'etre implementee silencieusement) : contrairement aux
    boutons (delete_action, qui refuse si l'action est encore utilisee dans
    un entry_actions), aucune table de ce schema ne reference une
    connection_id ailleurs -- une connexion n'est pas rattachee a une
    conversation ni a un workflow n8n specifique, elle est juste
    selectionnee au moment de l'envoi (voir workspace_routes.send_message_route).
    Il n'existe donc pas de dependance technique reelle a verifier ici au-
    dela de la confirmation utilisateur (deja geree cote frontend, prompt
    §26) : fabriquer une fausse verification d'usage irait a l'encontre de
    la consigne "ne simule jamais ce qui n'existe pas"."""
    with _db() as conn:
        row = conn.execute("SELECT created_by FROM connections WHERE id = %s;", (connection_id,)).fetchone()
        if not row:
            return False
        if row["created_by"] != actor_user_id and not is_admin:
            raise PermissionError("Seul le createur ou un administrateur peut supprimer cette connexion.")
        conn.execute("DELETE FROM connections WHERE id = %s;", (connection_id,))
        return True
