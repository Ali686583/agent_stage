"""
librairies/workflow_bank.py
============================

Banque centrale de boutons/actions et de references vers des workflows n8n
(Phase 5 de l'espace collaboratif). Persistee dans une base PostgreSQL
SEPAREE de celle des utilisateurs/conversations : le service Railway
"Postgres-jg_R", conformement a la repartition demandee ("Postgres-jg_R
pour la banque de workflow, Postgres pour tout le reste").

Aucune donnee d'identite (mot de passe, session, etc.) ne transite jamais
par ce module ; les references a un utilisateur ou une conversation sont de
simples chaines opaques (l'id tel qu'il existe dans la base principale),
jamais une cle etrangere SQL entre les deux bases (impossible entre deux
instances Postgres separees, et volontairement evite ici).

n8n reste la seule source de verite sur le CONTENU reel d'un workflow :
cette base ne stocke qu'une reference (n8n_workflow_id) et des metadonnees,
jamais la definition du workflow lui-meme.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

BANK_DATABASE_URL = os.environ.get("WORKFLOW_BANK_DATABASE_URL", "")


@contextmanager
def _db():
    if not BANK_DATABASE_URL:
        raise RuntimeError(
            "WORKFLOW_BANK_DATABASE_URL n'est pas configuree : voir librairies/workflow_bank.py."
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


def init_bank_db() -> None:
    """Cree les tables si elles n'existent pas encore. A appeler au demarrage."""
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_records (
                id             TEXT PRIMARY KEY,
                n8n_workflow_id TEXT,
                name           TEXT NOT NULL,
                webhook_path   TEXT UNIQUE,
                status         TEXT NOT NULL DEFAULT 'draft',
                metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actions (
                id                 TEXT PRIMARY KEY,
                name               TEXT NOT NULL,
                workflow_record_id TEXT REFERENCES workflow_records(id),
                created_by         TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'active',
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entry_actions (
                id         TEXT PRIMARY KEY,
                action_id  TEXT NOT NULL REFERENCES actions(id),
                context_id TEXT NOT NULL,
                alias      TEXT,
                position   INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entry_actions_context ON entry_actions (context_id, position);"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entry_actions_unique ON entry_actions (context_id, action_id);"
        )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _public_workflow_record(row) -> dict:
    return {
        "id": row["id"],
        "n8nWorkflowId": row["n8n_workflow_id"],
        "name": row["name"],
        "status": row["status"],
        "webhookPath": row["webhook_path"],
        "editorUrl": row["metadata"].get("editorUrl") if row["metadata"] else None,
    }


def _public_action(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "workflowRecordId": row["workflow_record_id"],
        "createdBy": row["created_by"],
        "status": row["status"],
        "createdAt": row["created_at"].isoformat(),
    }


def _public_entry_action(row) -> dict:
    return {
        "id": row["id"],
        "actionId": row["action_id"],
        "contextId": row["context_id"],
        "alias": row["alias"],
        "displayName": row["alias"] or row["action_name"],
        "actionName": row["action_name"],
        "actionStatus": row["action_status"],
        "position": row["position"],
    }


# ---------------------------------------------------------------------------
# Workflow records
# ---------------------------------------------------------------------------

def create_workflow_record(name: str, n8n_workflow_id: str | None, webhook_path: str, status: str, editor_url: str | None) -> dict:
    record_id = str(uuid.uuid4())
    with _db() as conn:
        row = conn.execute(
            """
            INSERT INTO workflow_records (id, n8n_workflow_id, name, webhook_path, status, metadata)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *;
            """,
            (record_id, n8n_workflow_id, name, webhook_path, status, Jsonb({"editorUrl": editor_url})),
        ).fetchone()
        return _public_workflow_record(row)


def get_workflow_record(workflow_record_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM workflow_records WHERE id = %s;", (workflow_record_id,)).fetchone()
        return _public_workflow_record(row) if row else None


def get_workflow_record_by_action(action_id: str) -> dict | None:
    """Renvoie le workflow_record associe a une action (jointure directe)."""
    with _db() as conn:
        row = conn.execute(
            """
            SELECT wr.* FROM workflow_records wr
            JOIN actions a ON a.workflow_record_id = wr.id
            WHERE a.id = %s;
            """,
            (action_id,),
        ).fetchone()
        return _public_workflow_record(row) if row else None


# ---------------------------------------------------------------------------
# Actions (banque centrale)
# ---------------------------------------------------------------------------

def create_action(name: str, created_by: str, workflow_record_id: str) -> dict:
    action_id = str(uuid.uuid4())
    with _db() as conn:
        row = conn.execute(
            """
            INSERT INTO actions (id, name, workflow_record_id, created_by)
            VALUES (%s, %s, %s, %s)
            RETURNING *;
            """,
            (action_id, name, workflow_record_id, created_by),
        ).fetchone()
        return _public_action(row)


def get_action(action_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM actions WHERE id = %s;", (action_id,)).fetchone()
        return _public_action(row) if row else None


def list_actions(search: str = "", limit: int = 50) -> list:
    """Banque consultable : recherche par nom (sous-chaine, insensible a la casse)."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM actions
            WHERE status = 'active' AND name ILIKE %s
            ORDER BY name ASC
            LIMIT %s;
            """,
            (f"%{search}%", limit),
        ).fetchall()
        return [_public_action(row) for row in rows]


def is_action_used_elsewhere(action_id: str, exclude_context_id: str | None = None) -> bool:
    with _db() as conn:
        if exclude_context_id is not None:
            row = conn.execute(
                "SELECT 1 FROM entry_actions WHERE action_id = %s AND context_id != %s LIMIT 1;",
                (action_id, exclude_context_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT 1 FROM entry_actions WHERE action_id = %s LIMIT 1;", (action_id,)).fetchone()
        return row is not None


def delete_action(action_id: str) -> bool:
    """Suppression definitive : refuse si l'action est encore utilisee quelque part."""
    with _db() as conn:
        used = conn.execute("SELECT 1 FROM entry_actions WHERE action_id = %s LIMIT 1;", (action_id,)).fetchone()
        if used:
            return False
        conn.execute("DELETE FROM actions WHERE id = %s;", (action_id,))
        return True


# ---------------------------------------------------------------------------
# Boutons affiches sous une entry (entry_actions)
# ---------------------------------------------------------------------------

def list_entry_actions(context_id: str) -> list:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT ea.*, a.name AS action_name, a.status AS action_status
            FROM entry_actions ea
            JOIN actions a ON a.id = ea.action_id
            WHERE ea.context_id = %s
            ORDER BY ea.position ASC, ea.created_at ASC;
            """,
            (context_id,),
        ).fetchall()
        return [_public_entry_action(row) for row in rows]


def add_entry_action(context_id: str, action_id: str, created_by: str, alias: str | None = None) -> dict:
    """Ajoute une association ; reutilise l'existante si deja presente (pas de doublon)."""
    with _db() as conn:
        existing = conn.execute(
            """
            SELECT ea.*, a.name AS action_name, a.status AS action_status
            FROM entry_actions ea JOIN actions a ON a.id = ea.action_id
            WHERE ea.context_id = %s AND ea.action_id = %s;
            """,
            (context_id, action_id),
        ).fetchone()
        if existing:
            return _public_entry_action(existing)

        next_position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos FROM entry_actions WHERE context_id = %s;",
            (context_id,),
        ).fetchone()["next_pos"]

        entry_action_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO entry_actions (id, action_id, context_id, alias, position, created_by)
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (entry_action_id, action_id, context_id, alias, next_position, created_by),
        )
        row = conn.execute(
            """
            SELECT ea.*, a.name AS action_name, a.status AS action_status
            FROM entry_actions ea JOIN actions a ON a.id = ea.action_id
            WHERE ea.id = %s;
            """,
            (entry_action_id,),
        ).fetchone()
        return _public_entry_action(row)


def remove_entry_action(entry_action_id: str, context_id: str) -> bool:
    """Enleve UNIQUEMENT l'association de cette entry ; l'action et son workflow restent dans la banque."""
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM entry_actions WHERE id = %s AND context_id = %s;",
            (entry_action_id, context_id),
        )
        return cur.rowcount > 0


def rename_entry_action_alias(entry_action_id: str, context_id: str, alias: str | None) -> bool:
    """Renomme localement (alias) sans jamais toucher au nom de l'action centrale partagee."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE entry_actions SET alias = %s WHERE id = %s AND context_id = %s;",
            (alias, entry_action_id, context_id),
        )
        return cur.rowcount > 0


def reorder_entry_actions(context_id: str, ordered_entry_action_ids: list) -> None:
    with _db() as conn:
        for index, entry_action_id in enumerate(ordered_entry_action_ids):
            conn.execute(
                "UPDATE entry_actions SET position = %s WHERE id = %s AND context_id = %s;",
                (index, entry_action_id, context_id),
            )
