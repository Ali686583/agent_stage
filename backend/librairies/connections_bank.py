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
get_connection_secret() ne la dechiffre. Toute fonction qui peut etre
appelee (directement ou indirectement) depuis une route HTTP renvoie
uniquement _public_connection(), qui ne contient jamais la clef.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from librairies.crypto_secrets import decrypt_secret, encrypt_secret

BANK_DATABASE_URL = os.environ.get("WORKFLOW_BANK_DATABASE_URL", "")

MAX_KEYWORDS = 15
MAX_KEYWORD_LENGTH = 40


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
            platform_type    TEXT NOT NULL,
            keywords         TEXT[] NOT NULL DEFAULT '{}',
            config           JSONB NOT NULL DEFAULT '{}'::jsonb,
            secret_encrypted TEXT NOT NULL,
            created_by       TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_connections_status ON connections (status);")


# ---------------------------------------------------------------------------
# Serialisation -- ne contient JAMAIS secret_encrypted ni la clef en clair.
# ---------------------------------------------------------------------------

def _public_connection(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "platformType": row["platform_type"],
        "keywords": list(row["keywords"] or []),
        "hasBaseUrl": bool((row["config"] or {}).get("baseUrl")),
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
    fournie par le frontend) : voir librairies/platform_client.py pour leur
    usage (adaptateur REST generique)."""
    config = config or {}
    cleaned: dict = {}
    base_url = str(config.get("baseUrl") or "").strip()[:500]
    if base_url.startswith("http://") or base_url.startswith("https://"):
        cleaned["baseUrl"] = base_url
    return cleaned


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_connection(
    name: str,
    platform_type: str,
    api_key: str,
    created_by: str,
    keywords: list | None = None,
    config: dict | None = None,
) -> dict:
    connection_id = str(uuid.uuid4())
    secret_encrypted = encrypt_secret(api_key)
    with _db() as conn:
        row = conn.execute(
            """
            INSERT INTO connections (id, name, platform_type, keywords, config, secret_encrypted, created_by)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING *;
            """,
            (
                connection_id,
                name,
                platform_type,
                _clean_keywords(keywords),
                Jsonb(_clean_config(config)),
                secret_encrypted,
                created_by,
            ),
        ).fetchone()
        return _public_connection(row)


def list_connections(search: str = "", limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM connections
            WHERE status = 'active' AND name ILIKE %s
            ORDER BY name ASC
            LIMIT %s;
            """,
            (f"%{search}%", limit),
        ).fetchall()
        return [_public_connection(row) for row in rows]


def list_connections_by_ids(connection_ids: list[str]) -> list[dict]:
    if not connection_ids:
        return []
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM connections WHERE id = ANY(%s) AND status = 'active';",
            (connection_ids,),
        ).fetchall()
        return [_public_connection(row) for row in rows]


def get_connection(connection_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = %s AND status = 'active';", (connection_id,)
        ).fetchone()
        return _public_connection(row) if row else None


def get_connection_secret(connection_id: str) -> tuple[dict, str] | None:
    """Usage strictement interne (librairies/platform_client.py, appele
    depuis le worker) : renvoie (connexion_publique, clef_en_clair), ou None
    si introuvable. Ne JAMAIS exposer cette fonction via une route HTTP."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = %s AND status = 'active';", (connection_id,)
        ).fetchone()
        if not row:
            return None
        return _internal_connection(row), decrypt_secret(row["secret_encrypted"])


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
