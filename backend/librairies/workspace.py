"""
librairies/workspace.py
========================

Couche de persistance de l'espace collaboratif IA (page affichee juste
apres connexion) : projets, conversations, messages, fichiers, resultats
reutilisables et executions de workflow n8n.

Meme principe que database.py : toute requete SQL vit ici, jamais dans les
routes (backend/workspace_routes.py). Reutilise la meme connexion Postgres
que le reste de l'application.

L'identite (user_id, author_name) est toujours fournie par l'appelant a
partir de la session authentifiee (backend/workspace_routes.py) : ce module
ne fait jamais confiance a une valeur venue du navigateur.
"""

from __future__ import annotations

import logging
import uuid

from psycopg.types.json import Jsonb

from librairies import realtime
from librairies.database import _db

_logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 80
MAX_PROJECT_NAME_LENGTH = 120
MAX_PROJECT_DESCRIPTION_LENGTH = 2000


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def derive_title(message: str) -> str:
    cleaned = " ".join((message or "").split())
    if not cleaned:
        return "Nouvelle discussion"
    if len(cleaned) <= MAX_TITLE_LENGTH:
        return cleaned
    truncated = cleaned[:MAX_TITLE_LENGTH].rsplit(" ", 1)[0]
    return (truncated or cleaned[:MAX_TITLE_LENGTH]) + "…"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _avatar_url(row: dict, avatar_key: str = "live_avatar") -> str | None:
    ref = row.get(avatar_key)
    user_id = row.get("live_user_id") or row.get("created_by_user_id") or row.get("user_id")
    return f"/api/auth/avatar/{user_id}" if ref and user_id else None


def _public_project(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "createdByUserId": row["created_by_user_id"],
        # Identite AFFICHEE resolue au moment de la lecture (pseudonyme
        # actuel, ou email si aucun) : jamais le snapshot fige en base.
        "createdByName": row.get("live_name") or row["created_by_name"],
        "createdByAvatarUrl": _avatar_url(row),
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
        "conversationCount": row.get("conversation_count", 0),
        "isShared": bool(row.get("is_shared")),
        "description": row.get("description"),
    }


def _public_conversation(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "createdByUserId": row["created_by_user_id"],
        "createdByName": row.get("live_name") or row["created_by_name"],
        "createdByAvatarUrl": _avatar_url(row),
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
        "isShared": bool(row.get("is_shared")),
    }


def _reply_excerpt(content: str | None, has_attachments: bool, max_length: int = 160) -> str:
    cleaned = " ".join((content or "").split())
    if not cleaned:
        return "[Pièce jointe]" if has_attachments else ""
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[:max_length].rsplit(" ", 1)[0] + "…"


def _public_message(row: dict) -> dict:
    reply_to_id = row.get("reply_to_message_id")
    reply_to = None
    if reply_to_id:
        # La jointure (voir _MESSAGE_SELECT) ne trouve rien si le message
        # cible n'existe plus : jamais suppose "aucune reponse" pour autant
        # (voir mission §13, "message reply original supprime") -- le
        # frontend doit pouvoir afficher un etat neutre plutot que rien.
        if row.get("reply_to_author_name") is not None:
            reply_to = {
                "id": reply_to_id,
                "authorName": row.get("reply_to_live_name") or row.get("reply_to_author_name"),
                "role": row.get("reply_to_role"),
                "excerpt": _reply_excerpt(row.get("reply_to_content"), bool(row.get("reply_to_has_attachment"))),
                "unavailable": False,
            }
        else:
            reply_to = {"id": reply_to_id, "unavailable": True}
    return {
        "id": row["id"],
        "conversationId": row["conversation_id"],
        "userId": row["user_id"],
        # Pour un message assistant (user_id NULL), pas de jointure : on
        # garde le nom fige ("ChatGPT"/"Claude", qui ne change jamais).
        "authorName": row.get("live_name") or row["author_name"],
        "authorAvatarUrl": _avatar_url(row) if row["user_id"] else None,
        "role": row["role"],
        "content": row["content"],
        "blocks": row["blocks"],
        "model": row["model"],
        "actionId": row["action_id"],
        "resultId": row["result_id"],
        "replyToMessageId": reply_to_id,
        "replyTo": reply_to,
        "seq": row["seq"],
        "createdAt": row["created_at"].isoformat(),
    }


def _public_file(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["original_name"],
        "mimeType": row["mime_type"],
        "size": row["size_bytes"],
        "uploadedByUserId": row["uploaded_by_user_id"],
        "createdAt": row["created_at"].isoformat(),
    }


def _public_result(row: dict) -> dict:
    return {
        "id": row["id"],
        "conversationId": row["conversation_id"],
        "messageId": row["message_id"],
        "userId": row["user_id"],
        "model": row["model"],
        "workflowType": row["workflow_type"],
        "request": row["request"],
        "response": row["response"],
        "status": row["status"],
        "syncStatus": row["sync_status"],
        "sourceResultIds": row["source_result_ids"],
        "metadata": row["metadata"],
        "createdAt": row["created_at"].isoformat(),
    }


def _public_participant(row: dict) -> dict:
    return {
        "userId": row["user_id"],
        "role": row["role"],
        "displayName": row.get("live_name") or row["user_id"],
        "avatarUrl": _avatar_url(row, avatar_key="live_avatar"),
        "addedAt": row["added_at"].isoformat(),
    }


def _public_workflow_run(row: dict) -> dict:
    return {
        "id": row["id"],
        "conversationId": row["conversation_id"],
        "messageId": row["message_id"],
        "userId": row["user_id"],
        "resultId": row["result_id"],
        "workflowType": row["workflow_type"],
        "requestId": row["request_id"],
        "n8nExecutionId": row["n8n_execution_id"],
        "status": row["status"],
        "startedAt": row["started_at"].isoformat(),
        "completedAt": row["completed_at"].isoformat() if row["completed_at"] else None,
        "error": row["error"],
    }


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

def list_projects() -> list[dict]:
    """Projets personnels (is_shared=false) : voir list_shared_projects()
    pour l'espace partage equivalent aux discussions communes."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT p.*, count(DISTINCT pc.conversation_id) AS conversation_count,
                   COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
            FROM projects p
            LEFT JOIN project_conversations pc ON pc.project_id = p.id
            LEFT JOIN users u ON u.id = p.created_by_user_id
            WHERE p.is_shared = false
            GROUP BY p.id, u.display_name, u.email, u.avatar_reference
            ORDER BY p.updated_at DESC
            """
        ).fetchall()
    return [_public_project(r) for r in rows]


def list_shared_projects() -> list[dict]:
    """Projets "communs" (is_shared=true) : meme principe que
    list_shared_conversations -- visibles par tous, jamais filtres par
    participation prealable, puisqu'un projet commun n'a pas de notion de
    "participant" propre (seules les discussions qu'il contient en ont une,
    et une discussion commune auto-rejoint tout le monde a l'ouverture)."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT p.*, count(DISTINCT pc.conversation_id) AS conversation_count,
                   COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
            FROM projects p
            LEFT JOIN project_conversations pc ON pc.project_id = p.id
            LEFT JOIN users u ON u.id = p.created_by_user_id
            WHERE p.is_shared = true
            GROUP BY p.id, u.display_name, u.email, u.avatar_reference
            ORDER BY p.updated_at DESC
            """
        ).fetchall()
    return [_public_project(r) for r in rows]


def _clean_project_description(description: str | None) -> str | None:
    """Description facultative (mission "Projets communs" §2) : une valeur
    vide/absente est toujours acceptee (ne bloque jamais la creation ni
    l'enregistrement), jamais requise contrairement au nom."""
    if description is None:
        return None
    cleaned = description.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_PROJECT_DESCRIPTION_LENGTH:
        raise ValueError(f"La description doit faire moins de {MAX_PROJECT_DESCRIPTION_LENGTH} caracteres.")
    return cleaned


def create_project(
    user_id: str, user_name: str, name: str, is_shared: bool = False, description: str | None = None
) -> dict:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Le nom du projet ne peut pas etre vide.")
    if len(cleaned) > MAX_PROJECT_NAME_LENGTH:
        raise ValueError(f"Le nom du projet doit faire moins de {MAX_PROJECT_NAME_LENGTH} caracteres.")
    cleaned_description = _clean_project_description(description)
    project_id = new_id("proj")
    with _db() as conn:
        conn.execute(
            """INSERT INTO projects (id, name, created_by_user_id, created_by_name, is_shared, description)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (project_id, cleaned, user_id, user_name, is_shared, cleaned_description),
        )
    return {
        "id": project_id,
        "name": cleaned,
        "createdByUserId": user_id,
        "createdByName": user_name,
        "conversationCount": 0,
        "isShared": is_shared,
        "description": cleaned_description,
    }


def project_exists(project_id: str) -> bool:
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone()
    return row is not None


def get_project(project_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            """
            SELECT p.*, count(DISTINCT pc.conversation_id) AS conversation_count,
                   COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
            FROM projects p
            LEFT JOIN project_conversations pc ON pc.project_id = p.id
            LEFT JOIN users u ON u.id = p.created_by_user_id
            WHERE p.id = %s
            GROUP BY p.id, u.display_name, u.email, u.avatar_reference
            """,
            (project_id,),
        ).fetchone()
    return _public_project(row) if row else None


def rename_project(project_id: str, user_id: str, name: str, description: str | None = None) -> dict:
    """Renomme le projet et, si `description` est fourni (meme methode que
    pour name/instruction des boutons partages : cf. workflow_bank.py),
    met aussi a jour sa description. `description=None` signifie "champ non
    envoye par le client" et laisse la valeur en base inchangee -- pour
    l'effacer explicitement, l'appelant doit envoyer une chaine vide."""
    cleaned = (name or "").strip().replace("<", "").replace(">", "")
    if not cleaned:
        raise ValueError("Le nom du projet ne peut pas etre vide.")
    if len(cleaned) > MAX_PROJECT_NAME_LENGTH:
        raise ValueError(f"Le nom du projet doit faire moins de {MAX_PROJECT_NAME_LENGTH} caracteres.")
    with _db() as conn:
        row = conn.execute(
            "SELECT created_by_user_id, description FROM projects WHERE id = %s", (project_id,)
        ).fetchone()
        if not row:
            raise LookupError("Projet introuvable.")
        if row["created_by_user_id"] != user_id:
            raise PermissionError("Seul le createur peut renommer ce projet.")
        if description is None:
            new_description = row["description"]
        else:
            new_description = _clean_project_description(description)
        conn.execute(
            "UPDATE projects SET name = %s, description = %s, updated_at = now() WHERE id = %s",
            (cleaned, new_description, project_id),
        )
    return get_project(project_id)


def list_project_conversations(
    project_id: str, user_id: str, limit: int = 30, before: str | None = None
) -> list[dict]:
    """Meme filtre d'appartenance que list_conversations : un projet peut
    contenir des conversations ajoutees par d'autres participants, on ne
    renvoie jamais celles dont `user_id` n'est pas participant."""
    limit = max(1, min(limit, 100))
    with _db() as conn:
        if before:
            rows = conn.execute(
                """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
                   FROM conversations c
                   JOIN project_conversations pc ON pc.conversation_id = c.id
                   JOIN conversation_participants cp ON cp.conversation_id = c.id AND cp.user_id = %s
                   LEFT JOIN users u ON u.id = c.created_by_user_id
                   WHERE pc.project_id = %s AND c.updated_at < %s
                   ORDER BY c.updated_at DESC LIMIT %s""",
                (user_id, project_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
                   FROM conversations c
                   JOIN project_conversations pc ON pc.conversation_id = c.id
                   JOIN conversation_participants cp ON cp.conversation_id = c.id AND cp.user_id = %s
                   LEFT JOIN users u ON u.id = c.created_by_user_id
                   WHERE pc.project_id = %s
                   ORDER BY c.updated_at DESC LIMIT %s""",
                (user_id, project_id, limit),
            ).fetchall()
    return [_public_conversation(r) for r in rows]


class InvalidAssociationError(ValueError):
    """Sous-classe de ValueError levee par add_conversation_to_project quand
    la conversation ne peut pas rejoindre ce type de projet (conversation
    privee -> projet commun) : distincte d'un simple "introuvable" pour que
    la route puisse renvoyer 400 (erreur de validation) plutot que 404, tout
    en restant capturable comme un ValueError generique par du code plus
    ancien qui ne la distinguerait pas explicitement."""


def add_conversation_to_project(project_id: str, conversation_id: str, user_id: str) -> None:
    with _db() as conn:
        project_row = conn.execute(
            "SELECT is_shared FROM projects WHERE id = %s", (project_id,)
        ).fetchone()
        if not project_row:
            raise ValueError("Projet introuvable.")
        conv_row = conn.execute(
            "SELECT is_shared FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
        if not conv_row:
            raise ValueError("Conversation introuvable.")
        # Un projet commun ne doit jamais pouvoir exposer l'existence d'une
        # conversation privee : sa liste de discussions (voir
        # list_shared_project_conversations) n'est pas filtree par
        # participation, contrairement a un projet personnel -- l'y ajouter
        # rendrait donc cette conversation privee visible de tous.
        if project_row["is_shared"] and not conv_row["is_shared"]:
            raise InvalidAssociationError("Seules les discussions communes peuvent etre ajoutees a un projet commun.")
        conn.execute(
            """INSERT INTO project_conversations (project_id, conversation_id, added_by_user_id)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (project_id, conversation_id, user_id),
        )
        conn.execute("UPDATE projects SET updated_at = now() WHERE id = %s", (project_id,))


def move_conversation_to_project(
    from_project_id: str, to_project_id: str, conversation_id: str, user_id: str
) -> None:
    """Deplace (pas duplique) une conversation d'un projet vers un autre :
    retire l'association a from_project_id et ajoute celle a to_project_id,
    dans la MEME transaction (jamais un etat intermediaire ou elle
    n'appartiendrait a aucun des deux si une des deux etapes echouait).
    Reutilise les memes garde-fous que add_conversation_to_project (types
    personnel/commun compatibles) : aucune nouvelle regle de permission
    inventee, cette fonction est un ADD + REMOVE de l'association deja
    existante, jamais un nouveau mecanisme d'acces."""
    with _db() as conn:
        to_project_row = conn.execute(
            "SELECT is_shared FROM projects WHERE id = %s", (to_project_id,)
        ).fetchone()
        if not to_project_row:
            raise ValueError("Projet de destination introuvable.")
        conv_row = conn.execute(
            "SELECT is_shared FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
        if not conv_row:
            raise ValueError("Conversation introuvable.")
        if to_project_row["is_shared"] != conv_row["is_shared"]:
            raise InvalidAssociationError(
                "Le type de discussion (personnelle/commune) ne correspond pas a celui du projet de destination."
            )
        if to_project_id == from_project_id:
            return  # rien a faire : deja dans ce projet
        conn.execute(
            "DELETE FROM project_conversations WHERE project_id = %s AND conversation_id = %s",
            (from_project_id, conversation_id),
        )
        conn.execute(
            """INSERT INTO project_conversations (project_id, conversation_id, added_by_user_id)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (to_project_id, conversation_id, user_id),
        )
        conn.execute(
            "UPDATE projects SET updated_at = now() WHERE id = ANY(%s)",
            ([from_project_id, to_project_id],),
        )


def list_shared_project_conversations(project_id: str) -> list[dict]:
    """Equivalent de list_project_conversations pour un projet COMMUN :
    jamais filtre par participation prealable (meme principe que
    list_shared_conversations), puisque toute discussion qui peut s'y
    trouver est necessairement elle-meme commune (voir la garde ajoutee
    dans add_conversation_to_project ci-dessus)."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
               FROM conversations c
               JOIN project_conversations pc ON pc.conversation_id = c.id
               LEFT JOIN users u ON u.id = c.created_by_user_id
               WHERE pc.project_id = %s
               ORDER BY c.updated_at DESC""",
            (project_id,),
        ).fetchall()
    return [_public_conversation(r) for r in rows]


def delete_project(project_id: str, user_id: str) -> bool:
    """Supprime un projet et ses associations project_conversations
    (ON DELETE CASCADE) : les conversations elles-memes NE sont PAS
    supprimees, elles restent dans Discussions. Seul le createur peut
    supprimer. Renvoie False si le projet n'existe pas."""
    with _db() as conn:
        row = conn.execute(
            "SELECT created_by_user_id, is_shared FROM projects WHERE id = %s", (project_id,)
        ).fetchone()
        if not row:
            return False
        # Meme garde que pour une conversation commune (delete_conversation
        # ci-dessous) : un projet commun n'appartient a personne en
        # particulier, meme son createur d'origine ne peut pas le supprimer
        # pour tout le monde.
        if row["is_shared"]:
            raise PermissionError("Le projet commun ne peut pas etre supprime.")
        if row["created_by_user_id"] != user_id:
            raise PermissionError("Seul le createur peut supprimer ce projet.")
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
    return True


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def list_conversations(user_id: str, limit: int = 30, before: str | None = None) -> list[dict]:
    """Ne renvoie que les conversations dont `user_id` est participant :
    connaitre/deviner un conversation_id ne suffit jamais, et cette liste ne
    doit jamais fuiter l'existence de conversations d'autres utilisateurs.

    La conversation commune (is_shared) est volontairement exclue d'ici :
    elle a deja son propre acces dedie dans l'interface ("Discussion
    commune"), et l'afficher EN PLUS dans cette liste normale a cause un
    bug reel en production (confusion -> suppression accidentelle de la
    conversation commune via le menu "..." de son entree en double)."""
    limit = max(1, min(limit, 100))
    with _db() as conn:
        if before:
            rows = conn.execute(
                """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
                   FROM conversations c
                   JOIN conversation_participants cp ON cp.conversation_id = c.id AND cp.user_id = %s
                   LEFT JOIN users u ON u.id = c.created_by_user_id
                   WHERE c.updated_at < %s AND c.is_shared = false
                   ORDER BY c.updated_at DESC LIMIT %s""",
                (user_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
                   FROM conversations c
                   JOIN conversation_participants cp ON cp.conversation_id = c.id AND cp.user_id = %s
                   LEFT JOIN users u ON u.id = c.created_by_user_id
                   WHERE c.is_shared = false
                   ORDER BY c.updated_at DESC LIMIT %s""",
                (user_id, limit),
            ).fetchall()
    return [_public_conversation(r) for r in rows]


def get_conversation(conversation_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
               FROM conversations c
               LEFT JOIN users u ON u.id = c.created_by_user_id
               WHERE c.id = %s""",
            (conversation_id,),
        ).fetchone()
    return _public_conversation(row) if row else None


def create_conversation(user_id: str, user_name: str, title: str, project_id: str | None = None) -> dict:
    conversation_id = new_id("conv")
    with _db() as conn:
        conn.execute(
            """INSERT INTO conversations (id, title, created_by_user_id, created_by_name)
               VALUES (%s, %s, %s, %s)""",
            (conversation_id, title[:MAX_TITLE_LENGTH], user_id, user_name),
        )
        conn.execute(
            """INSERT INTO conversation_participants (conversation_id, user_id, role)
               VALUES (%s, %s, 'owner')""",
            (conversation_id, user_id),
        )
        if project_id:
            if not conn.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone():
                raise ValueError("Projet introuvable.")
            conn.execute(
                """INSERT INTO project_conversations (project_id, conversation_id, added_by_user_id)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (project_id, conversation_id, user_id),
            )
    return get_conversation(conversation_id)


def list_shared_conversations() -> list[dict]:
    """
    Toutes les discussions "communes" (is_shared=true), quel que soit leur
    createur : n'importe quel utilisateur authentifie peut les voir et les
    ouvrir (l'acces reel est verifie/auto-accorde par is_participant a
    l'ouverture, jamais ici). Contrairement aux conversations normales,
    cette liste n'est PAS filtree par participation prealable : une
    discussion commune est par definition ouverte a tous.
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT c.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
               FROM conversations c
               LEFT JOIN users u ON u.id = c.created_by_user_id
               WHERE c.is_shared = true
               ORDER BY c.updated_at DESC"""
        ).fetchall()
    return [_public_conversation(r) for r in rows]


def create_shared_conversation(user_id: str, user_name: str, title: str) -> dict:
    """Cree une nouvelle discussion commune (is_shared=true). Plusieurs
    peuvent coexister : chaque utilisateur peut en creer une, elle est
    immediatement visible et rejoignable par tous (voir list_shared_
    conversations et l'auto-jointure dans is_participant)."""
    conversation_id = new_id("conv")
    with _db() as conn:
        conn.execute(
            """INSERT INTO conversations (id, title, created_by_user_id, created_by_name, is_shared)
               VALUES (%s, %s, %s, %s, true)""",
            (conversation_id, title[:MAX_TITLE_LENGTH], user_id, user_name),
        )
        conn.execute(
            """INSERT INTO conversation_participants (conversation_id, user_id, role)
               VALUES (%s, %s, 'owner')""",
            (conversation_id, user_id),
        )
    return get_conversation(conversation_id)


def touch_conversation(conversation_id: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

def is_participant(conversation_id: str, user_id: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, user_id),
        ).fetchone()
        if row:
            return True
        # Conversation commune ("session commune" demandee explicitement) :
        # n'importe quel utilisateur authentifie en devient automatiquement
        # participant des son premier acces, plutot que d'exiger une
        # invitation explicite comme pour les conversations normales.
        shared = conn.execute(
            "SELECT is_shared FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
        if shared and shared["is_shared"]:
            conn.execute(
                """INSERT INTO conversation_participants (conversation_id, user_id, role)
                   VALUES (%s, %s, 'member') ON CONFLICT DO NOTHING""",
                (conversation_id, user_id),
            )
            return True
    return False


def list_participants(conversation_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT cp.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar
               FROM conversation_participants cp
               JOIN users u ON u.id = cp.user_id
               WHERE cp.conversation_id = %s
               ORDER BY cp.added_at ASC""",
            (conversation_id,),
        ).fetchall()
    return [_public_participant(r) for r in rows]


def add_participant(conversation_id: str, actor_user_id: str, target_user_id: str) -> list[dict]:
    """Ajoute target_user_id a la conversation. Seul un participant deja
    'owner' peut inviter quelqu'un (le simple fait d'etre 'member' ne suffit
    pas)."""
    with _db() as conn:
        actor_row = conn.execute(
            "SELECT role FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, actor_user_id),
        ).fetchone()
        if not actor_row:
            raise LookupError("Conversation introuvable.")
        if actor_row["role"] != "owner":
            raise PermissionError("Seul le proprietaire peut ajouter un participant.")
        if not conn.execute("SELECT 1 FROM users WHERE id = %s", (target_user_id,)).fetchone():
            raise ValueError("Utilisateur introuvable.")
        conn.execute(
            """INSERT INTO conversation_participants (conversation_id, user_id, role)
               VALUES (%s, %s, 'member') ON CONFLICT DO NOTHING""",
            (conversation_id, target_user_id),
        )
    return list_participants(conversation_id)


def remove_participant(conversation_id: str, actor_user_id: str, target_user_id: str) -> list[dict]:
    """Retire target_user_id. Seul un 'owner' peut retirer quelqu'un
    d'autre ; un 'owner' ne peut jamais se retirer lui-meme s'il est le
    dernier owner restant (la conversation deviendrait orpheline)."""
    with _db() as conn:
        actor_row = conn.execute(
            "SELECT role FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, actor_user_id),
        ).fetchone()
        if not actor_row:
            raise LookupError("Conversation introuvable.")
        if actor_row["role"] != "owner":
            raise PermissionError("Seul le proprietaire peut retirer un participant.")
        if target_user_id == actor_user_id:
            remaining_owners = conn.execute(
                """SELECT count(*) AS n FROM conversation_participants
                   WHERE conversation_id = %s AND role = 'owner' AND user_id != %s""",
                (conversation_id, target_user_id),
            ).fetchone()
            if remaining_owners["n"] == 0:
                raise PermissionError("Impossible de se retirer : dernier proprietaire de la conversation.")
        conn.execute(
            "DELETE FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, target_user_id),
        )
    return list_participants(conversation_id)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def add_message(
    conversation_id: str,
    user_id: str | None,
    author_name: str,
    role: str,
    content: str,
    blocks: list | None = None,
    model: str | None = None,
    action_id: str | None = None,
    result_id: str | None = None,
    reply_to_message_id: str | None = None,
    publish_extra: dict | None = None,
) -> dict:
    message_id = new_id("msg")
    with _db() as conn:
        conn.execute(
            """INSERT INTO messages
               (id, conversation_id, user_id, author_name, role, content, blocks, model, action_id, result_id,
                reply_to_message_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                message_id,
                conversation_id,
                user_id,
                author_name,
                role,
                content,
                Jsonb(blocks) if blocks is not None else None,
                model,
                action_id,
                result_id,
                reply_to_message_id,
            ),
        )
    created = get_message(message_id)
    try:
        # publish_extra (ex. requestId) n'est ajoute qu'a l'evenement temps
        # reel, jamais persiste : sert uniquement au frontend a rattacher la
        # reponse asynchrone a la bonne requete en attente (Phase 4).
        event_payload = dict(created, **publish_extra) if publish_extra else created
        realtime.publish_event(conversation_id, "message.created", event_payload)
    except Exception:
        # Best-effort : le message est deja persiste en Postgres (source de
        # verite) ; un Redis indisponible ne doit jamais faire echouer
        # l'envoi, seul le push temps reel est perdu (rattrapable via seq).
        _logger.warning("publish_event a echoue pour la conversation %s", conversation_id, exc_info=True)
    return created


def publish_workflow_failed(conversation_id: str, request_id: str, message_id: str, error: str) -> None:
    """Evenement ephemere (jamais persiste) : previent les clients connectes
    qu'une demande en attente a echoue, pour qu'ils resolvent leur indicateur
    de chargement au lieu d'attendre indefiniment. Voir librairies/jobs.py."""
    realtime.publish_event(
        conversation_id, "workflow.failed", {"requestId": request_id, "messageId": message_id, "error": error}
    )


_MESSAGE_SELECT = """
    SELECT m.*, COALESCE(u.display_name, u.email) AS live_name, u.avatar_reference AS live_avatar,
           rt.author_name AS reply_to_author_name, rt.content AS reply_to_content, rt.role AS reply_to_role,
           COALESCE(ru.display_name, ru.email) AS reply_to_live_name,
           EXISTS(SELECT 1 FROM message_attachments ma WHERE ma.message_id = rt.id) AS reply_to_has_attachment
    FROM messages m
    LEFT JOIN users u ON u.id = m.user_id
    LEFT JOIN messages rt ON rt.id = m.reply_to_message_id
    LEFT JOIN users ru ON ru.id = rt.user_id
"""


def get_message(message_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(_MESSAGE_SELECT + " WHERE m.id = %s", (message_id,)).fetchone()
    return _public_message(row) if row else None


def list_messages(conversation_id: str, limit: int = 50, before: str | None = None) -> list[dict]:
    limit = max(1, min(limit, 200))
    with _db() as conn:
        if before:
            rows = conn.execute(
                _MESSAGE_SELECT + " WHERE m.conversation_id = %s AND m.created_at < %s"
                " ORDER BY m.created_at DESC LIMIT %s",
                (conversation_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                _MESSAGE_SELECT + " WHERE m.conversation_id = %s"
                " ORDER BY m.created_at DESC LIMIT %s",
                (conversation_id, limit),
            ).fetchall()
    rows.reverse()
    return [_public_message(r) for r in rows]


def list_messages_after(conversation_id: str, after_seq: int) -> list[dict]:
    """Rattrapage SSE : tous les messages strictement posterieurs a
    after_seq, dans l'ordre. Utilise a la connexion (after_seq = plus grand
    seq deja recu par le client) et a la reconnexion (after_seq derive du
    header Last-Event-ID envoye automatiquement par EventSource)."""
    with _db() as conn:
        rows = conn.execute(
            _MESSAGE_SELECT + " WHERE m.conversation_id = %s AND m.seq > %s ORDER BY m.seq ASC",
            (conversation_id, after_seq),
        ).fetchall()
    return [_public_message(r) for r in rows]


def rename_conversation(conversation_id: str, user_id: str, title: str) -> dict:
    cleaned = (title or "").strip().replace("<", "").replace(">", "")
    if not cleaned:
        raise ValueError("Le titre de la conversation ne peut pas etre vide.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"Le titre de la conversation doit faire moins de {MAX_TITLE_LENGTH} caracteres.")
    with _db() as conn:
        row = conn.execute(
            "SELECT created_by_user_id FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
        if not row:
            raise LookupError("Conversation introuvable.")
        if row["created_by_user_id"] != user_id:
            raise PermissionError("Seul le createur peut renommer cette conversation.")
        conn.execute(
            "UPDATE conversations SET title = %s, updated_at = now() WHERE id = %s",
            (cleaned, conversation_id),
        )
    return get_conversation(conversation_id)


def delete_conversation(conversation_id: str, user_id: str) -> bool:
    """Supprime une conversation et tout ce qui lui est rattache (messages,
    pieces jointes, resultats, workflow_runs, associations aux projets) via
    les contraintes ON DELETE CASCADE deja definies sur ces tables. Seul le
    createur peut supprimer. Renvoie False si la conversation n'existe pas."""
    with _db() as conn:
        row = conn.execute(
            "SELECT created_by_user_id, is_shared FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
        if not row:
            return False
        # La conversation commune n'appartient a personne en particulier :
        # meme son createur d'origine ne peut pas la supprimer pour tout le
        # monde. Bug reel observe en production sans cette garde : elle a
        # ete supprimee par megarde (elle apparaissait aussi dans la liste
        # normale des discussions), effacant son historique pour tous les
        # utilisateurs qui la partageaient.
        if row["is_shared"]:
            raise PermissionError("La conversation commune ne peut pas etre supprimee.")
        if row["created_by_user_id"] != user_id:
            raise PermissionError("Seul le createur peut supprimer cette conversation.")
        conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
    return True


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------

def create_file_record(
    file_id: str, user_id: str, original_name: str, mime_type: str, size_bytes: int, storage_reference: str
) -> dict:
    with _db() as conn:
        conn.execute(
            """INSERT INTO files (id, original_name, mime_type, size_bytes, storage_reference, uploaded_by_user_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (file_id, original_name, mime_type, size_bytes, storage_reference, user_id),
        )
    return get_file(file_id)


def get_file(file_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = %s", (file_id,)).fetchone()
    return _public_file(row) if row else None


def rename_file(file_id: str, user_id: str, name: str) -> dict:
    """Renomme uniquement le nom d'AFFICHAGE (original_name). file_id et
    storage_reference ne changent jamais : aucun autre enregistrement
    (message_attachments, workflows n8n en cours, liens signes deja emis)
    n'est donc affecte."""
    cleaned = (name or "").strip().replace("<", "").replace(">", "")
    if not cleaned:
        raise ValueError("Le nom du fichier ne peut pas etre vide.")
    if len(cleaned) > 200:
        raise ValueError("Le nom du fichier doit faire moins de 200 caracteres.")
    with _db() as conn:
        row = conn.execute(
            "SELECT uploaded_by_user_id FROM files WHERE id = %s", (file_id,)
        ).fetchone()
        if not row:
            raise LookupError("Fichier introuvable.")
        if row["uploaded_by_user_id"] != user_id:
            raise PermissionError("Seul l'auteur du fichier peut le renommer.")
        conn.execute("UPDATE files SET original_name = %s WHERE id = %s", (cleaned, file_id))
    return get_file(file_id)


def get_file_storage_reference(file_id: str) -> str | None:
    with _db() as conn:
        row = conn.execute("SELECT storage_reference FROM files WHERE id = %s", (file_id,)).fetchone()
    return row["storage_reference"] if row else None


def user_can_access_file(file_id: str, user_id: str) -> bool:
    """Le proprietaire y a toujours accès ; une fois le fichier attache a un
    message (donc partage dans une conversation), tout utilisateur
    authentifie de cet espace collaboratif peut y accéder aussi."""
    with _db() as conn:
        row = conn.execute(
            "SELECT uploaded_by_user_id FROM files WHERE id = %s", (file_id,)
        ).fetchone()
        if not row:
            return False
        if row["uploaded_by_user_id"] == user_id:
            return True
        attached = conn.execute(
            "SELECT 1 FROM message_attachments WHERE file_id = %s LIMIT 1", (file_id,)
        ).fetchone()
        return attached is not None


def link_files_to_message(message_id: str, file_ids: list[str]) -> None:
    with _db() as conn:
        for file_id in file_ids:
            conn.execute(
                """INSERT INTO message_attachments (id, message_id, file_id)
                   VALUES (%s, %s, %s)""",
                (new_id("att"), message_id, file_id),
            )


def list_message_attachments(message_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT f.* FROM message_attachments ma
               JOIN files f ON f.id = ma.file_id
               WHERE ma.message_id = %s""",
            (message_id,),
        ).fetchall()
    return [_public_file(r) for r in rows]


def list_orphan_files_for_user(user_id: str) -> list[dict]:
    """Fichiers uploades par cet utilisateur mais jamais rattaches a un
    message (donc jamais partages dans une conversation) : purement
    personnels. Utilise lors de la suppression de compte pour ne nettoyer
    reellement que les donnees strictement personnelles."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT f.* FROM files f
               WHERE f.uploaded_by_user_id = %s
                 AND NOT EXISTS (SELECT 1 FROM message_attachments ma WHERE ma.file_id = f.id)""",
            (user_id,),
        ).fetchall()
    return [_public_file(r) for r in rows]


def delete_files(file_ids: list[str]) -> None:
    if not file_ids:
        return
    with _db() as conn:
        conn.execute("DELETE FROM files WHERE id = ANY(%s)", (file_ids,))


# ---------------------------------------------------------------------------
# Resultats reutilisables
# ---------------------------------------------------------------------------

def create_result(
    conversation_id: str,
    message_id: str,
    user_id: str,
    model: str,
    workflow_type: str,
    request: dict,
    response: dict,
    source_result_ids: list[str] | None = None,
    metadata: dict | None = None,
    sync_status: str = "synced",
) -> dict:
    result_id = new_id("res")
    with _db() as conn:
        conn.execute(
            """INSERT INTO results
               (id, conversation_id, message_id, user_id, model, workflow_type,
                request, response, source_result_ids, metadata, sync_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                result_id,
                conversation_id,
                message_id,
                user_id,
                model,
                workflow_type,
                Jsonb(request),
                Jsonb(response),
                source_result_ids or [],
                Jsonb(metadata) if metadata is not None else None,
                sync_status,
            ),
        )
    return get_result(result_id)


def get_result(result_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM results WHERE id = %s", (result_id,)).fetchone()
    return _public_result(row) if row else None


# ---------------------------------------------------------------------------
# Executions de workflow (idempotence + tracabilite)
# ---------------------------------------------------------------------------

def start_workflow_run(
    conversation_id: str, message_id: str | None, user_id: str, workflow_type: str, request_id: str
) -> dict | None:
    """Cree un run en 'running'. Renvoie None si ce request_id a deja ete vu
    (rejoue reseau, double-clic) : l'appelant doit alors relire le run
    existant via get_workflow_run_by_request_id plutot que rappeler n8n."""
    run_id = new_id("run")
    with _db() as conn:
        if conn.execute(
            "SELECT 1 FROM workflow_runs WHERE request_id = %s", (request_id,)
        ).fetchone():
            return None
        conn.execute(
            """INSERT INTO workflow_runs
               (id, conversation_id, message_id, user_id, workflow_type, request_id, status)
               VALUES (%s,%s,%s,%s,%s,%s,'running')""",
            (run_id, conversation_id, message_id, user_id, workflow_type, request_id),
        )
    return {"id": run_id}


def retry_workflow_run(request_id: str) -> str | None:
    """Reutilise un run existant tombe en echec/timeout (meme request_id)
    pour une nouvelle tentative, plutot que d'en creer un second - request_id
    est UNIQUE. Renvoie l'id du run remis en 'running', ou None si aucun run
    failed/timeout ne correspond (deja termine, ou toujours en cours)."""
    with _db() as conn:
        row = conn.execute(
            """UPDATE workflow_runs
               SET status = 'running', error = NULL, completed_at = NULL, started_at = now()
               WHERE request_id = %s AND status IN ('failed', 'timeout')
               RETURNING id""",
            (request_id,),
        ).fetchone()
    return row["id"] if row else None


def complete_workflow_run(
    run_id: str,
    status: str,
    result_id: str | None = None,
    error: str | None = None,
    n8n_execution_id: str | None = None,
) -> None:
    with _db() as conn:
        conn.execute(
            """UPDATE workflow_runs
               SET status = %s, result_id = %s, error = %s, n8n_execution_id = %s, completed_at = now()
               WHERE id = %s""",
            (status, result_id, error, n8n_execution_id, run_id),
        )


def get_workflow_run_by_request_id(request_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE request_id = %s", (request_id,)
        ).fetchone()
    return _public_workflow_run(row) if row else None
