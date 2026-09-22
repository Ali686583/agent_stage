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

# Identifiant FIXE (pas un uuid genere) : cette action integree est reconnue
# explicitement par librairies/jobs.py (traitement special Google Drive,
# jamais route vers un webhook n8n comme les autres boutons de la banque -
# voir ensure_google_drive_action ci-dessous et execute_workflow_run).
GOOGLE_DRIVE_ACTION_ID = "builtin-google-drive-summary"
# Veille web par mots-cles (mission "Veille Web") : deux boutons distincts,
# meme raison que pour Google Drive (id fixe reconnu par librairies/jobs.py,
# jamais route vers un webhook n8n).
WEB_MONITORING_FREE_ACTION_ID = "builtin-web-monitoring-free"
# Id de base de donnees conserve tel quel (deja deploye en production) meme
# si ce bouton n'utilise plus Tavily -- voir ensure_web_monitoring_actions,
# qui met a jour son nom/sa description affiches sans changer son id.
WEB_MONITORING_GENERAL_ACTION_ID = "builtin-web-monitoring-tavily"
_SYSTEM_CREATED_BY = "system"


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
    """
    Cree les tables si elles n'existent pas encore. A appeler au demarrage.

    Verrou consultatif Postgres autour de la creation : avec plusieurs
    workers gunicorn (-w 2) qui importent ce module en meme temps au tout
    premier demarrage (tables encore inexistantes), deux `CREATE TABLE IF
    NOT EXISTS` concurrents peuvent tous les deux passer le test d'existence
    avant que l'un des deux ne committe, et Postgres leve alors une
    UniqueViolation sur son catalogue interne (pg_type) au lieu d'ignorer
    silencieusement la creation en double. Observe en production sur cette
    meme base : le crash du worker perdant a fait redemarrer tout le
    conteneur, et seul le redemarrage suivant (tables deja creees par
    l'autre worker) a reussi. Le verrou serialise cette section pour que ça
    n'arrive plus jamais, meme sur une base totalement vierge.
    """
    with _db() as conn:
        conn.execute("SELECT pg_advisory_lock(727001727001);")
        _create_tables(conn)
        conn.execute("SELECT pg_advisory_unlock(727001727001);")


def _create_tables(conn) -> None:
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
        # Migration additive (mission "boutons SPS") : metadonnees etendues
        # d'un bouton partage, au-dela du simple nom -- description affichee,
        # instruction/prompt systeme de reference (jamais un secret, donc pas
        # besoin du systeme de credentials pour ce champ), integrations et
        # fichiers requis, comportement de routage modele, et verrouillage
        # optimiste (version) pour que deux editions concurrentes du meme
        # bouton partage ne s'ecrasent jamais silencieusement (voir
        # update_action_details ci-dessous).
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS description TEXT;")
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS instruction TEXT;")
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS required_integrations JSONB NOT NULL DEFAULT '[]'::jsonb;")
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS required_files JSONB NOT NULL DEFAULT '[]'::jsonb;")
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS model_routing TEXT NOT NULL DEFAULT 'respects_selector';")
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;")
        conn.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS last_edited_by TEXT;")
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
        "description": row.get("description"),
        "instruction": row.get("instruction"),
        "requiredIntegrations": row.get("required_integrations") or [],
        "requiredFiles": row.get("required_files") or [],
        "modelRouting": row.get("model_routing") or "respects_selector",
        "version": row.get("version", 1),
        "lastEditedBy": row.get("last_edited_by"),
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
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
        # Permet au frontend de proposer "Ouvrir dans n8n" directement sur ce
        # bouton, sans avoir a rechercher le workflow manuellement (spec Phase 5 §11).
        "editorUrl": row.get("editor_url"),
        # Id brut du createur du BOUTON (pas de l'association entry_action) :
        # cette base ne connait jamais les noms d'utilisateurs (voir
        # l'entete du module) -- workspace_routes.py resout ce id en nom
        # affichable via librairies/database.get_users_by_ids avant de
        # renvoyer la reponse au frontend (UI : "Createur : ...").
        "actionCreatedBy": row.get("action_created_by"),
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


def delete_workflow_record(workflow_record_id: str) -> str | None:
    """Supprime la reference locale a un workflow n8n (utilise par
    delete_action_bank_route juste apres delete_action, pour ne pas laisser
    une ligne orpheline dans Postgres-jg_R en plus du workflow n8n lui-meme
    -- trouve pendant la validation E2E de la suppression d'un bouton :
    delete_action() ne touchait jusqu'ici que la table actions, jamais
    workflow_records ni le workflow n8n reel, qui restaient actifs et
    joignables indefiniment). Renvoie le n8n_workflow_id a nettoyer cote n8n
    (voir workspace_routes.py, qui appelle n8n_client.delete_workflow dessus
    -- ce module ne connait jamais N8N_API_URL/N8N_API_KEY lui-meme), ou None
    si la ligne n'existait pas ou n'avait pas de workflow n8n associe."""
    with _db() as conn:
        row = conn.execute(
            "DELETE FROM workflow_records WHERE id = %s RETURNING n8n_workflow_id;",
            (workflow_record_id,),
        ).fetchone()
        return row["n8n_workflow_id"] if row else None


# ---------------------------------------------------------------------------
# Actions (banque centrale)
# ---------------------------------------------------------------------------

def create_action(
    name: str,
    created_by: str,
    workflow_record_id: str,
    description: str | None = None,
    instruction: str | None = None,
    required_integrations: list | None = None,
    required_files: list | None = None,
    model_routing: str = "respects_selector",
) -> dict:
    action_id = str(uuid.uuid4())
    with _db() as conn:
        row = conn.execute(
            """
            INSERT INTO actions
                (id, name, workflow_record_id, created_by, description, instruction,
                 required_integrations, required_files, model_routing)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            RETURNING *;
            """,
            (
                action_id,
                name,
                workflow_record_id,
                created_by,
                description,
                instruction,
                Jsonb(required_integrations or []),
                Jsonb(required_files or []),
                model_routing,
            ),
        ).fetchone()
        return _public_action(row)


def ensure_action_with_id(action_id: str, name: str, description: str | None = None) -> dict:
    """Cree une action a un id FIXE (idempotent - appele a chaque demarrage,
    voir ensure_google_drive_action/ensure_web_monitoring_actions), ou
    resynchronise son nom/sa description si elle existe deja -- ces boutons
    integres sont geres par le code, pas par un utilisateur, donc leur
    affichage doit toujours refleter la version deployee (ex. le
    renommage "Tavily" -> "recherche generale" sans changer d'id, voir
    WEB_MONITORING_GENERAL_ACTION_ID). Contrairement a create_action,
    workflow_record_id reste NULL : cette action n'a pas de workflow n8n
    (voir la note d'architecture dans le module jobs.py qui la traite
    specialement), et la colonne le permet deja (pas de NOT NULL)."""
    with _db() as conn:
        existing = conn.execute("SELECT * FROM actions WHERE id = %s;", (action_id,)).fetchone()
        if existing:
            row = conn.execute(
                "UPDATE actions SET name = %s, description = %s, updated_at = now() WHERE id = %s RETURNING *;",
                (name, description, action_id),
            ).fetchone()
            return _public_action(row)
        row = conn.execute(
            """
            INSERT INTO actions (id, name, workflow_record_id, created_by, description)
            VALUES (%s, %s, NULL, %s, %s)
            RETURNING *;
            """,
            (action_id, name, _SYSTEM_CREATED_BY, description),
        ).fetchone()
        return _public_action(row)


def ensure_google_drive_action() -> dict:
    """Enregistre (une seule fois, idempotent) le bouton integre "Resume
    Drive" dans la banque de boutons partagee, et le rend immediatement
    disponible sous l'entry (comme n'importe quel autre bouton de la
    banque -- mission §5 : "disponible dans la banque de boutons"). Appelee
    au demarrage du serveur (voir server.py)."""
    action = ensure_action_with_id(
        GOOGLE_DRIVE_ACTION_ID,
        name="Résumé Drive",
        description="Résume un document Google Drive (collez son lien ou son identifiant dans votre demande).",
    )
    add_entry_action(context_id="shared", action_id=GOOGLE_DRIVE_ACTION_ID, created_by=_SYSTEM_CREATED_BY)
    return action


def ensure_web_monitoring_actions() -> None:
    """Enregistre (et resynchronise a chaque demarrage) les deux boutons
    integres de veille web par mots-cles -- deux sources gratuites, sans
    compte ni cle API (decision explicite apres avoir ecarte Tavily, qui
    demandait une carte bancaire meme sur son offre gratuite) : le flux RSS
    Google Actualites (actualites) et une recherche generale via
    DuckDuckGo (voir librairies/web_search.py)."""
    ensure_action_with_id(
        WEB_MONITORING_FREE_ACTION_ID,
        name="Veille Web (sans API)",
        description=(
            "Recherche des articles publics recents par mots-cles via le flux RSS "
            "public de Google Actualites. Aucune cle API requise."
        ),
    )
    add_entry_action(context_id="shared", action_id=WEB_MONITORING_FREE_ACTION_ID, created_by=_SYSTEM_CREATED_BY)

    ensure_action_with_id(
        WEB_MONITORING_GENERAL_ACTION_ID,
        name="Veille Web (recherche générale)",
        description=(
            "Recherche generale (pas seulement des actualites) sur des pages publiques "
            "via DuckDuckGo. Aucune cle API ni compte requis."
        ),
    )
    add_entry_action(context_id="shared", action_id=WEB_MONITORING_GENERAL_ACTION_ID, created_by=_SYSTEM_CREATED_BY)


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


def force_delete_action(action_id: str) -> bool:
    """Suppression definitive en un clic (decision produit, bug "Supprimer
    definitivement" : SHARED_BUTTON_CONTEXT_ID etant le seul contexte
    d'entry_actions, un bouton atteignable depuis la recherche y est
    quasi-toujours encore reference, donc delete_action() refusait presque
    systematiquement avec un 409 -- feedback quasi invisible cote UI, voir
    workspace.js showComposerError). Ce nouveau chemin detache l'action de
    TOUS les contextes qui la referencent puis la supprime, sans jamais
    refuser -- meme forme de transaction que purge_actions_named ci-dessous,
    juste bornee a un seul id. Additif : delete_action()/
    is_action_used_elsewhere() restent inchangees, toujours utilisees
    ailleurs (ex. flux de mise a jour de version)."""
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM actions WHERE id = %s;", (action_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM entry_actions WHERE action_id = %s;", (action_id,))
        conn.execute("DELETE FROM actions WHERE id = %s;", (action_id,))
        return True


class VersionConflictError(RuntimeError):
    """Levee quand expected_version ne correspond plus a la version en base :
    quelqu'un d'autre a deja modifie ce bouton partage entre-temps. L'appelant
    doit relire la version actuelle et laisser l'utilisateur reappliquer son
    edition consciemment, jamais ecraser silencieusement (mission §12)."""


def update_action_details(
    action_id: str,
    actor_user_id: str,
    expected_version: int,
    name: str | None = None,
    description: str | None = None,
    instruction: str | None = None,
    required_integrations: list | None = None,
    required_files: list | None = None,
) -> dict:
    """Edition partagee d'un bouton (nom/description/instruction/...) avec
    verrouillage optimiste : la mise a jour n'est appliquee que si `version`
    en base vaut encore `expected_version` (lu par l'appelant juste avant).
    Sinon : VersionConflictError, jamais un ecrasement silencieux d'une
    modification plus recente faite par un autre utilisateur (mission §12).
    N'importe quel utilisateur authentifie peut editer un bouton partage
    (meme modele de confiance que le reste de la banque -- aucun systeme de
    permissions granulaire n'existe dans cette application ; "droits
    d'edition" = etre un utilisateur authentifie de cet espace collaboratif)."""
    with _db() as conn:
        current = conn.execute("SELECT * FROM actions WHERE id = %s;", (action_id,)).fetchone()
        if not current:
            raise LookupError("Bouton introuvable.")
        if current["version"] != expected_version:
            raise VersionConflictError(
                f"Ce bouton a ete modifie entre-temps (version actuelle {current['version']}, "
                f"attendue {expected_version})."
            )
        row = conn.execute(
            """
            UPDATE actions
            SET name = COALESCE(%s, name),
                description = %s,
                instruction = %s,
                required_integrations = %s::jsonb,
                required_files = %s::jsonb,
                last_edited_by = %s,
                version = version + 1,
                updated_at = now()
            WHERE id = %s AND version = %s
            RETURNING *;
            """,
            (
                name,
                description if description is not None else current["description"],
                instruction if instruction is not None else current["instruction"],
                Jsonb(required_integrations if required_integrations is not None else (current["required_integrations"] or [])),
                Jsonb(required_files if required_files is not None else (current["required_files"] or [])),
                actor_user_id,
                action_id,
                expected_version,
            ),
        ).fetchone()
        if not row:
            # Course tres etroite entre le SELECT ci-dessus et cet UPDATE :
            # quelqu'un d'autre vient de committer une modification entre les
            # deux. Meme traitement qu'un conflit detecte plus tot.
            raise VersionConflictError("Ce bouton a ete modifie entre-temps par quelqu'un d'autre.")
        return _public_action(row)


# ---------------------------------------------------------------------------
# Boutons affiches sous une entry (entry_actions)
# ---------------------------------------------------------------------------

def list_entry_actions(context_id: str) -> list:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT ea.*, a.name AS action_name, a.status AS action_status, a.created_by AS action_created_by,
                   wr.metadata->>'editorUrl' AS editor_url
            FROM entry_actions ea
            JOIN actions a ON a.id = ea.action_id
            LEFT JOIN workflow_records wr ON wr.id = a.workflow_record_id
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
            SELECT ea.*, a.name AS action_name, a.status AS action_status, a.created_by AS action_created_by,
                   wr.metadata->>'editorUrl' AS editor_url
            FROM entry_actions ea
            JOIN actions a ON a.id = ea.action_id
            LEFT JOIN workflow_records wr ON wr.id = a.workflow_record_id
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
            SELECT ea.*, a.name AS action_name, a.status AS action_status, a.created_by AS action_created_by,
                   wr.metadata->>'editorUrl' AS editor_url
            FROM entry_actions ea
            JOIN actions a ON a.id = ea.action_id
            LEFT JOIN workflow_records wr ON wr.id = a.workflow_record_id
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


# ---------------------------------------------------------------------------
# Nettoyage ponctuel demande explicitement (bouton de demonstration
# "Analyser pdf" cree pendant la preuve a 2 comptes de cette session, plus
# besoin desormais). Supprime FORCE les entry_actions qui le referencent
# (contrairement a delete_action, qui refuse normalement si encore utilise
# ailleurs : ici la suppression EST le but explicite), puis l'action et son
# workflow_record. Renvoie la liste des n8n_workflow_id a nettoyer cote n8n
# (voir server.py, qui appelle n8n_client.delete_workflow dessus) : ce
# module ne connait jamais N8N_API_URL/N8N_API_KEY lui-meme.
# ---------------------------------------------------------------------------

def purge_actions_named(names: list[str]) -> list[str]:
    if not names:
        return []
    with _db() as conn:
        actions = conn.execute(
            "SELECT id, workflow_record_id FROM actions WHERE name = ANY(%s);",
            (names,),
        ).fetchall()
        if not actions:
            return []
        action_ids = [a["id"] for a in actions]
        workflow_record_ids = [a["workflow_record_id"] for a in actions if a["workflow_record_id"]]
        conn.execute("DELETE FROM entry_actions WHERE action_id = ANY(%s);", (action_ids,))
        conn.execute("DELETE FROM actions WHERE id = ANY(%s);", (action_ids,))
        n8n_workflow_ids = []
        if workflow_record_ids:
            records = conn.execute(
                "SELECT n8n_workflow_id FROM workflow_records WHERE id = ANY(%s);",
                (workflow_record_ids,),
            ).fetchall()
            n8n_workflow_ids = [r["n8n_workflow_id"] for r in records if r["n8n_workflow_id"]]
            conn.execute("DELETE FROM workflow_records WHERE id = ANY(%s);", (workflow_record_ids,))
        return n8n_workflow_ids
