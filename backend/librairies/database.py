"""
librairies/database.py
=======================

Couche de persistance du backend agent_stage, basee sur PostgreSQL (fourni
par le plugin Postgres de Railway en production). Reference d'architecture :
librairies/database.py de MyBusiness (meme decoupage : une seule couche qui
parle a la base, le serveur ne fait jamais de SQL lui-meme).

Difference volontaire avec MyBusiness : les mots de passe sont haches avec
Argon2id (au lieu de PBKDF2-SHA256), les tokens de session et de
reinitialisation ne sont jamais stockes en clair (seule leur empreinte
SHA-256 est persistee), et le stockage est une vraie base Postgres
persistante plutot que SQLite sur disque local ephemere.
"""

from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from librairies.security import hash_password, verify_password, needs_rehash

DATABASE_URL = os.environ.get("DATABASE_URL", "")

RESET_TOKEN_TTL_MINUTES = int(os.environ.get("RESET_TOKEN_TTL_MINUTES", "30"))
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------

@contextmanager
def _db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL n'est pas configuree.")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """
    Cree les tables si elles n'existent pas encore. A appeler au demarrage.

    Verrou consultatif Postgres : avec plusieurs workers gunicorn qui
    importent ce module en meme temps, des CREATE TABLE/INDEX IF NOT EXISTS
    concurrents sur des objets pas encore crees peuvent tous les deux passer
    le test d'existence avant qu'aucun ne committe, et Postgres leve alors
    une UniqueViolation sur son catalogue interne au lieu d'ignorer
    silencieusement le doublon (deja observe en production sur la banque de
    boutons, meme cause). Ce verrou serialise toute la migration.
    """
    with _db() as conn:
        conn.execute("SELECT pg_advisory_lock(727001727002);")
        _create_core_tables(conn)
        conn.execute("SELECT pg_advisory_unlock(727001727002);")


def _create_core_tables(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # Migration additive : identite affichee (pseudonyme + avatar),
        # distincte de l'email de connexion qui ne change jamais ici.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_reference TEXT;")
        # Migration additive : role applicatif (standard/admin). Fondation
        # pour les permissions futures (Phase 1 de l'espace collaboratif) ;
        # aucune route n'en depend encore aujourd'hui.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'standard';")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                used_at    TIMESTAMPTZ
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reset_tokens_user ON password_reset_tokens (user_id);"
        )

        # -- Espace collaboratif IA (page apres connexion) -------------------
        # Tables additives uniquement : aucune des tables ci-dessus n'est
        # modifiee ni supprimee. Voir librairies/workspace.py pour l'usage.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id                 TEXT PRIMARY KEY,
                name               TEXT NOT NULL,
                created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_by_name    TEXT NOT NULL,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id                 TEXT PRIMARY KEY,
                title              TEXT NOT NULL DEFAULT '',
                created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_by_name    TEXT NOT NULL,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # Migration additive : conversation "commune" partagee entre tous les
        # utilisateurs (session commune demandee explicitement), distincte
        # d'une conversation privee avec invitation explicite (Phase 1).
        conn.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_shared BOOLEAN NOT NULL DEFAULT false;")
        # Reparation ponctuelle : une race condition (deux acces concurrents
        # au tout premier appel de "Discussion commune", cf. get_or_create_
        # shared_conversation) a pu creer plus d'une conversation marquee
        # is_shared=true avant la mise en place du verrou ci-dessous. On ne
        # garde que la plus ancienne comme conversation commune canonique ;
        # les autres redeviennent de simples conversations privees (leurs
        # messages et participants restent intacts, juste plus "communes").
        conn.execute(
            """
            UPDATE conversations SET is_shared = false
            WHERE is_shared = true
            AND id NOT IN (
                SELECT id FROM conversations WHERE is_shared = true
                ORDER BY created_at ASC LIMIT 1
            );
            """
        )
        # Verrou structurel : au plus UNE conversation is_shared=true peut
        # exister desormais (index unique partiel). Toute course future entre
        # deux requetes concurrentes echoue proprement au lieu de dupliquer
        # (voir ON CONFLICT dans get_or_create_shared_conversation).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_one_shared "
            "ON conversations (is_shared) WHERE is_shared = true;"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_participants (
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role            TEXT NOT NULL DEFAULT 'member',
                added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (conversation_id, user_id)
            );
            """
        )
        # Backfill idempotent : toute conversation deja existante (creee avant
        # l'introduction de cette table) obtient son createur comme 'owner'.
        # Sans cette ligne, les conversations anterieures deviendraient
        # inaccessibles a tout le monde (y compris leur createur) des que les
        # routes se mettent a verifier l'appartenance.
        conn.execute(
            """
            INSERT INTO conversation_participants (conversation_id, user_id, role)
            SELECT id, created_by_user_id, 'owner' FROM conversations
            ON CONFLICT (conversation_id, user_id) DO NOTHING;
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_conversations (
                project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                added_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (project_id, conversation_id)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id         TEXT REFERENCES users(id) ON DELETE SET NULL,
                author_name     TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL DEFAULT '',
                blocks          JSONB,
                model           TEXT,
                action_id       TEXT,
                result_id       TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # Migration additive : curseur monotone pour le rattrapage SSE (Phase 3).
        # created_at seul n'est pas fiable comme curseur (collisions possibles
        # a la microseconde pres) ; seq l'est toujours, y compris pour les
        # messages inseres avant cette migration (backfill automatique par
        # Postgres a la creation de la colonne).
        conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS seq BIGSERIAL;")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_seq ON messages (conversation_id, seq);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id                   TEXT PRIMARY KEY,
                original_name        TEXT NOT NULL,
                mime_type            TEXT NOT NULL,
                size_bytes           BIGINT NOT NULL,
                storage_reference    TEXT NOT NULL,
                uploaded_by_user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_attachments (
                id          TEXT PRIMARY KEY,
                message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                file_id     TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id                 TEXT PRIMARY KEY,
                conversation_id    TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                message_id         TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                model              TEXT NOT NULL,
                workflow_type      TEXT NOT NULL,
                request            JSONB NOT NULL,
                response           JSONB NOT NULL,
                status             TEXT NOT NULL DEFAULT 'completed',
                sync_status        TEXT NOT NULL DEFAULT 'synced',
                source_result_ids  TEXT[] NOT NULL DEFAULT '{}',
                metadata           JSONB,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id               TEXT PRIMARY KEY,
                conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                message_id       TEXT REFERENCES messages(id) ON DELETE SET NULL,
                user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                result_id        TEXT REFERENCES results(id) ON DELETE SET NULL,
                workflow_type    TEXT NOT NULL,
                request_id       TEXT NOT NULL UNIQUE,
                n8n_execution_id TEXT,
                status           TEXT NOT NULL DEFAULT 'queued',
                started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at     TIMESTAMPTZ,
                error            TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations (updated_at DESC);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, created_at);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_conversations_conv ON project_conversations (conversation_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_participants_user ON conversation_participants (user_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_attachments_message ON message_attachments (message_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_results_conversation ON results (conversation_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_uploader ON files (uploaded_by_user_id);"
        )


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------

def normalize_email(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if not _EMAIL_RE.match(cleaned):
        raise ValueError("Adresse email invalide.")
    if len(cleaned) > 254:
        raise ValueError("Adresse email invalide.")
    return cleaned


def normalize_username(value: str) -> str:
    cleaned = "".join(
        ch for ch in (value or "").strip().lower() if ch.isalnum() or ch in {"_", "-", "."}
    )
    if len(cleaned) < 3:
        raise ValueError("L'identifiant doit contenir au moins 3 caracteres.")
    return cleaned[:40]


def display_name_for(row: dict) -> str:
    """Identite AFFICHEE : le pseudonyme s'il existe, sinon l'email.
    Jamais utilisee comme identifiant technique (voir row['id'])."""
    return row.get("display_name") or row["email"]


def avatar_url_for(row: dict) -> str | None:
    return f"/api/auth/avatar/{row['id']}" if row.get("avatar_reference") else None


def _public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "displayName": display_name_for(row),
        "avatarUrl": avatar_url_for(row),
        "role": row.get("role") or "standard",
    }


# ---------------------------------------------------------------------------
# Comptes
# ---------------------------------------------------------------------------

def create_user(username: str, email: str, password: str) -> dict:
    normalized_username = normalize_username(username)
    normalized_email = normalize_email(email)
    if len(password) < 8:
        raise ValueError("Le mot de passe doit contenir au moins 8 caracteres.")

    user_id = str(uuid.uuid4())
    pwd_hash = hash_password(password)

    with _db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = %s OR email = %s",
            (normalized_username, normalized_email),
        ).fetchone()
        if exists:
            raise ValueError("Cet identifiant ou cette adresse email existe deja.")
        conn.execute(
            """INSERT INTO users (id, username, email, password_hash)
               VALUES (%s, %s, %s, %s)""",
            (user_id, normalized_username, normalized_email, pwd_hash),
        )
    return {
        "id": user_id,
        "username": normalized_username,
        "email": normalized_email,
        "displayName": normalized_email,
        "avatarUrl": None,
    }


def authenticate_user(identifier: str, password: str) -> dict | None:
    cleaned = (identifier or "").strip().lower()
    if not cleaned:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = %s OR email = %s",
            (cleaned, cleaned),
        ).fetchone()
        if not row:
            return None
        if not verify_password(row["password_hash"], password):
            return None
        if needs_rehash(row["password_hash"]):
            conn.execute(
                "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
                (hash_password(password), row["id"]),
            )
    return _public_user(row)


def get_user_by_id(user_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    return _public_user(row) if row else None


def get_user_by_identifier(identifier: str) -> dict | None:
    """Resout un utilisateur par identifiant OU email, sans mot de passe
    (meme lookup qu'authenticate_user). Utilise pour ajouter un participant
    a une conversation a partir de ce que son proprietaire saisit."""
    cleaned = (identifier or "").strip().lower()
    if not cleaned:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = %s OR email = %s",
            (cleaned, cleaned),
        ).fetchone()
    return _public_user(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (normalized,)).fetchone()
    return _public_user(row) if row else None


def set_password(user_id: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("Le mot de passe doit contenir au moins 8 caracteres.")
    with _db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
            (hash_password(new_password), user_id),
        )


# ---------------------------------------------------------------------------
# Profil (pseudonyme, avatar) et suppression de compte
# ---------------------------------------------------------------------------

_DISPLAY_NAME_MIN = 2
_DISPLAY_NAME_MAX = 40


def normalize_display_name(value: str) -> str:
    # Le frontend affiche toujours ce champ via textContent (jamais
    # interprete comme HTML) ; on retire quand meme les chevrons en defense
    # en profondeur, pour ne jamais persister quelque chose qui y ressemble.
    cleaned = (value or "").strip().replace("<", "").replace(">", "")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) < _DISPLAY_NAME_MIN:
        raise ValueError(f"Le pseudonyme doit contenir au moins {_DISPLAY_NAME_MIN} caracteres.")
    if len(cleaned) > _DISPLAY_NAME_MAX:
        raise ValueError(f"Le pseudonyme doit contenir au plus {_DISPLAY_NAME_MAX} caracteres.")
    return cleaned


def set_display_name(user_id: str, display_name: str) -> dict:
    cleaned = normalize_display_name(display_name)
    with _db() as conn:
        conn.execute(
            "UPDATE users SET display_name = %s, updated_at = now() WHERE id = %s",
            (cleaned, user_id),
        )
    return get_user_by_id(user_id)


def clear_display_name(user_id: str) -> dict:
    """Retire le pseudonyme : l'email redevient l'identite affichee."""
    with _db() as conn:
        conn.execute(
            "UPDATE users SET display_name = NULL, updated_at = now() WHERE id = %s",
            (user_id,),
        )
    return get_user_by_id(user_id)


def set_avatar_reference(user_id: str, avatar_reference: str) -> dict:
    with _db() as conn:
        conn.execute(
            "UPDATE users SET avatar_reference = %s, updated_at = now() WHERE id = %s",
            (avatar_reference, user_id),
        )
    return get_user_by_id(user_id)


def get_avatar_reference(user_id: str) -> str | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT avatar_reference FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    return row["avatar_reference"] if row else None


def delete_account(user_id: str) -> str | None:
    """Anonymise le compte plutot que de supprimer la ligne `users` :
    PAGE 3 est un espace collaboratif partage, donc les conversations,
    projets, messages et resultats crees par cet utilisateur restent
    references par d'autres (created_by_user_id, messages.user_id, etc.) et
    doivent survivre intacts a la suppression de SON compte.

    Seules les donnees strictement personnelles sont reellement supprimees :
    sessions actives, tokens de reset en attente, et le hash de mot de passe
    (remplace par une valeur aleatoire qu'aucun mot de passe ne peut jamais
    produire). L'email et l'identifiant sont remplaces par des valeurs
    "tombstone" uniques : l'ancien email redevient disponible pour un futur
    compte, et aucune connexion n'est plus jamais possible avec les anciens
    identifiants.

    Renvoie l'ancienne reference d'avatar (a supprimer du disque par
    l'appelant, cote fichiers), ou None si le compte n'existait pas.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT avatar_reference FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        if not row:
            return None
        old_avatar = row["avatar_reference"]

        tombstone_email = f"deleted-{user_id}@deleted.invalid"
        tombstone_username = f"deleted_{user_id.replace('-', '')[:20]}"
        unusable_hash = hash_password(secrets.token_urlsafe(32))

        conn.execute(
            """UPDATE users
               SET email = %s, username = %s, password_hash = %s,
                   display_name = 'Compte supprime', avatar_reference = NULL,
                   updated_at = now()
               WHERE id = %s""",
            (tombstone_email, tombstone_username, unusable_hash, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM password_reset_tokens WHERE user_id = %s", (user_id,))
    return old_avatar


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(user_id: str, token_hash: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    with _db() as conn:
        conn.execute(
            """INSERT INTO sessions (token_hash, user_id, expires_at)
               VALUES (%s, %s, %s)""",
            (token_hash, user_id, expires_at),
        )


def user_for_session(token_hash: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            """SELECT users.* FROM sessions
               JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = %s AND sessions.expires_at > now()""",
            (token_hash,),
        ).fetchone()
    return _public_user(row) if row else None


def delete_session(token_hash: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


def delete_all_sessions_for_user(user_id: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


# ---------------------------------------------------------------------------
# Reinitialisation de mot de passe
# ---------------------------------------------------------------------------

def create_reset_token(user_id: str, token_hash: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    with _db() as conn:
        conn.execute(
            """INSERT INTO password_reset_tokens (token_hash, user_id, expires_at)
               VALUES (%s, %s, %s)""",
            (token_hash, user_id, expires_at),
        )


def consume_reset_token(token_hash: str) -> str | None:
    """Valide un token de reset (existe, non expire, non deja utilise).

    Le marque comme utilise dans la foulee (usage unique) et renvoie
    l'identifiant de l'utilisateur concerne, ou None si invalide/expire.
    """
    with _db() as conn:
        row = conn.execute(
            """SELECT user_id FROM password_reset_tokens
               WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()""",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = now() WHERE token_hash = %s",
            (token_hash,),
        )
    return row["user_id"]


def invalidate_reset_tokens_for_user(user_id: str) -> None:
    """Invalide tous les tokens de reset non utilises d'un utilisateur."""
    with _db() as conn:
        conn.execute(
            """UPDATE password_reset_tokens SET used_at = now()
               WHERE user_id = %s AND used_at IS NULL""",
            (user_id,),
        )
