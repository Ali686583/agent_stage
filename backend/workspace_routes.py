"""
workspace_routes.py
====================

Routes de l'espace collaboratif IA (page affichee juste apres connexion) :
projets, conversations, messages, upload/telechargement de documents, et le
pont vers les workflows n8n (ChatGPT / Claude).

Architecture (voir le prompt qui a demande cette page) :
  PAGE -> RAILWAY (ici) -> N8N -> workflow ChatGPT/Claude -> RAILWAY -> PAGE.
Le navigateur n'appelle jamais n8n directement, et ne choisit jamais
librement un workflow : seuls les modeles/actions de l'allowlist ci-dessous
sont acceptes, valides ici avant tout appel sortant.

L'identite (user_id, author_name) est TOUJOURS derivee de la session
authentifiee (cookie), jamais d'une valeur envoyee par le navigateur dans
le corps de la requete.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import time
import uuid

import requests
from flask import Blueprint, Response, jsonify, redirect, request, send_file, stream_with_context

from librairies import connections_bank, database, google_drive, n8n_client, oauth_connector, realtime, workflow_bank, workspace
from librairies.google_drive import GoogleDriveConfigError, GoogleDriveError
from librairies.oauth_connector import OAuthConnectorConfigError, OAuthConnectorError
from librairies.jobs import execute_workflow_run, queue
from librairies.n8n_client import N8nConfigError
from librairies.rate_limit import limiter
from librairies.security import generate_token, hash_token, sign_file_token

SESSION_COOKIE_NAME = "agent_stage_session"

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_FILES_PER_MESSAGE = int(os.environ.get("MAX_FILES_PER_MESSAGE", "5"))
FILE_LINK_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))
N8N_TIMEOUT_SECONDS = int(os.environ.get("N8N_TIMEOUT_SECONDS", "90"))

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/msword",
    "image/png",
    "image/jpeg",
    "image/webp",
}

ALLOWED_MODELS = {"chatgpt", "claude"}
# Mode "Message" (mission §4) : jamais un provider IA, un simple message
# publie dans la discussion. Volontairement distinct de ALLOWED_MODELS (qui
# ne designe que des providers IA reellement appeles) pour que le sens de
# chaque ensemble reste evident a la lecture.
MESSAGE_MODE = "message"

GOOGLE_OAUTH_STATE_COOKIE = "agent_stage_gdrive_oauth_state"
# Connexions plateformes/API, entree 2 (OAuth generique, voir
# librairies/oauth_connector.py) : contrairement au cookie Google Drive
# ci-dessus, l'etat doit aussi vehiculer QUELLE connexion est en cours
# d'autorisation (une connexion generique n'est pas "l'utilisateur courant",
# voir _build_connections_oauth_state ci-dessous).
CONNECTIONS_OAUTH_STATE_COOKIE = "agent_stage_connections_oauth_state"

# Allowlist d'actions : le navigateur ne choisit jamais librement un workflow,
# seuls ces identifiants sont acceptes puis transmis a n8n.
ALLOWED_ACTIONS = {
    "analyze_document",
    "summarize_document",
    "compare_documents",
    "extract_data",
    "generate_chart",
    "generate_report",
}

# "Session commune" demandee explicitement : la banque de boutons est un
# contexte UNIQUE, partage par tous les utilisateurs authentifies (au lieu
# d'une bibliotheque personnelle par utilisateur). Consequence assumee et
# signalee : renommer (alias) ou enlever un bouton affecte tout le monde,
# puisqu'il n'existe plus qu'une seule ligne entry_actions par bouton, pas
# une par utilisateur.
SHARED_BUTTON_CONTEXT_ID = "shared"

# Cap d'entree large ; la validation reelle (longueur, contenu vide -> None)
# vit dans workspace.py (_clean_project_description), jamais dupliquee ici.
MAX_PROJECT_DESCRIPTION_INPUT_LENGTH = 4000

os.makedirs(UPLOAD_DIR, exist_ok=True)

workspace_bp = Blueprint("workspace", __name__, url_prefix="/api/workspace")


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------

def _error(status: int, message: str):
    return jsonify(ok=False, error=message), status


def _current_user():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return database.user_for_session(hash_token(token))


def _require_user():
    user = _current_user()
    if not user:
        return None, _error(401, "Non authentifie.")
    return user, None


def _safe_storage_name(file_id: str, original_name: str) -> str:
    _, ext = os.path.splitext(original_name)
    ext = "".join(ch for ch in ext if ch.isalnum() or ch == ".")[:10]
    return f"{file_id}{ext}"


def _serve_file(file_id: str):
    record = workspace.get_file(file_id)
    if not record:
        return _error(404, "Fichier introuvable.")
    storage_reference = workspace.get_file_storage_reference(file_id)
    storage_path = os.path.join(UPLOAD_DIR, storage_reference)
    if not os.path.isfile(storage_path):
        return _error(404, "Fichier introuvable.")
    return send_file(
        storage_path,
        mimetype=record["mimeType"],
        as_attachment=False,
        download_name=record["name"],
    )


# ---------------------------------------------------------------------------
# Affichage du createur d'un bouton (banque de boutons) : workflow_bank vit
# dans Postgres-jg_R, une base SEPAREE qui ne connait jamais les noms
# d'utilisateurs (voir l'entete de librairies/workflow_bank.py) -- la
# resolution id -> nom affichable se fait donc ici, cote glue HTTP, jamais
# simulee ni codee en dur cote frontend.
# ---------------------------------------------------------------------------

# Bouton integre (voir workflow_bank.ensure_google_drive_action) : n'a pas de
# compte utilisateur reel a resoudre, jamais affiche comme "inconnu" pour
# autant (qui laisserait croire a un createur supprime).
_SYSTEM_CREATOR_NAME = "Application"


def _resolve_creator_name(created_by: str | None, creator: dict | None) -> str | None:
    if created_by == "system":
        return _SYSTEM_CREATOR_NAME
    return creator["displayName"] if creator else None


def _with_creator_name(action: dict) -> dict:
    creator = database.get_user_by_id(action.get("createdBy"))
    action["createdByName"] = _resolve_creator_name(action.get("createdBy"), creator)
    return action


def _with_creator_names(actions: list) -> list:
    creators = database.get_users_by_ids([a.get("createdBy") for a in actions])
    for action in actions:
        action["createdByName"] = _resolve_creator_name(action.get("createdBy"), creators.get(action.get("createdBy")))
    return actions


def _with_entry_action_creator_names(entry_actions: list) -> list:
    creators = database.get_users_by_ids([ea.get("actionCreatedBy") for ea in entry_actions])
    for entry_action in entry_actions:
        entry_action["actionCreatedByName"] = _resolve_creator_name(
            entry_action.get("actionCreatedBy"), creators.get(entry_action.get("actionCreatedBy"))
        )
    return entry_actions


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

@workspace_bp.route("/projects", methods=["GET"])
def list_projects_route():
    _user, err = _require_user()
    if err:
        return err
    return jsonify(ok=True, projects=workspace.list_projects())


@workspace_bp.route("/projects", methods=["POST"])
@limiter.limit("20 per minute")
def create_project_route():
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))[:200]
    try:
        project = workspace.create_project(user["id"], user["displayName"], name)
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, project=project)


@workspace_bp.route("/projects/shared", methods=["GET"])
def list_shared_projects_route():
    """Projets "communs" : visibles et ouvrables par TOUS les utilisateurs
    authentifies, sans invitation (meme principe que /conversations/shared).
    Route statique enregistree AVANT /projects/<project_id> pour ne jamais
    en etre interceptee."""
    user, err = _require_user()
    if err:
        return err
    return jsonify(ok=True, projects=workspace.list_shared_projects())


@workspace_bp.route("/projects/shared", methods=["POST"])
@limiter.limit("10 per minute")
def create_shared_project_route():
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:200] or "Projet commun"
    description = data.get("description")
    if description is not None:
        description = str(description)[:MAX_PROJECT_DESCRIPTION_INPUT_LENGTH]
    try:
        project = workspace.create_project(
            user["id"], user["displayName"], name, is_shared=True, description=description
        )
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, project=project)


@workspace_bp.route("/projects/<project_id>/conversations", methods=["GET"])
def list_project_conversations_route(project_id):
    user, err = _require_user()
    if err:
        return err
    project = workspace.get_project(project_id)
    if not project:
        return _error(404, "Projet introuvable.")
    if project["isShared"]:
        return jsonify(ok=True, conversations=workspace.list_shared_project_conversations(project_id))
    limit = request.args.get("limit", default=30, type=int)
    before = request.args.get("before")
    return jsonify(
        ok=True,
        conversations=workspace.list_project_conversations(project_id, user["id"], limit, before),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@workspace_bp.route("/conversations", methods=["GET"])
def list_conversations_route():
    user, err = _require_user()
    if err:
        return err
    limit = request.args.get("limit", default=30, type=int)
    before = request.args.get("before")
    return jsonify(ok=True, conversations=workspace.list_conversations(user["id"], limit, before))


@workspace_bp.route("/conversations/shared", methods=["GET"])
def list_shared_conversations_route():
    """Discussions "communes" : visibles et ouvrables par TOUS les
    utilisateurs authentifies, sans invitation. Route statique enregistree
    AVANT la route dynamique /conversations/<conversation_id> pour ne
    jamais etre interceptee par elle."""
    user, err = _require_user()
    if err:
        return err
    return jsonify(ok=True, conversations=workspace.list_shared_conversations())


@workspace_bp.route("/conversations/shared", methods=["POST"])
@limiter.limit("10 per minute")
def create_shared_conversation_route():
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()[:200] or "Discussion commune"
    conversation = workspace.create_shared_conversation(user["id"], user["displayName"], title)
    return jsonify(ok=True, conversation=conversation)


@workspace_bp.route("/conversations/<conversation_id>", methods=["GET"])
def get_conversation_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        # 404 plutot que 403 : ne pas reveler qu'une conversation existe a
        # quelqu'un qui n'y a pas acces.
        return _error(404, "Conversation introuvable.")
    conversation = workspace.get_conversation(conversation_id)
    if not conversation:
        return _error(404, "Conversation introuvable.")
    limit = request.args.get("limit", default=50, type=int)
    before = request.args.get("before")
    messages = workspace.list_messages(conversation_id, limit, before)
    for message in messages:
        message["attachments"] = workspace.list_message_attachments(message["id"])
    return jsonify(ok=True, conversation=conversation, messages=messages)


@workspace_bp.route("/conversations/<conversation_id>/projects", methods=["POST"])
def add_conversation_to_project_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    data = request.get_json(silent=True) or {}
    project_id = str(data.get("projectId", ""))
    if not project_id:
        return _error(400, "projectId requis.")
    try:
        workspace.add_conversation_to_project(project_id, conversation_id, user["id"])
    except workspace.InvalidAssociationError as exc:
        return _error(400, str(exc))
    except ValueError as exc:
        return _error(404, str(exc))
    return jsonify(ok=True)


@workspace_bp.route("/conversations/<conversation_id>/move-project", methods=["POST"])
@limiter.limit("20 per minute")
def move_conversation_project_route(conversation_id):
    """"Deplacer dans un autre projet [commun]" (mission §6/7) : un seul
    endpoint pour les deux cas, comme add_conversation_to_project_route ci-
    dessus -- le type personnel/commun est verifie cote serveur (jamais
    confie au frontend), voir workspace.move_conversation_to_project.

    Permission (mission §7, "verifie les autorisations cote BACKEND") :
    etre participant de la discussion deplacee est deja obligatoire (comme
    pour toute action sur une conversation) ; pour une discussion COMMUNE,
    is_participant() rejoint automatiquement tout utilisateur authentifie
    (meme regle que le reste de l'application, voir workspace.is_participant)
    -- il n'existe pas de permission plus fine sur les projets communs dans
    cette application (aucune n'est demandee ailleurs), donc aucune n'est
    inventee ici : un utilisateur SANS acces a la discussion (donc jamais
    devenu participant) est bloque des ce premier controle, avant meme de
    savoir si le projet de destination existe."""
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    data = request.get_json(silent=True) or {}
    from_project_id = str(data.get("fromProjectId", ""))
    to_project_id = str(data.get("toProjectId", ""))
    if not from_project_id or not to_project_id:
        return _error(400, "fromProjectId et toProjectId requis.")
    try:
        workspace.move_conversation_to_project(from_project_id, to_project_id, conversation_id, user["id"])
    except workspace.InvalidAssociationError as exc:
        return _error(400, str(exc))
    except ValueError as exc:
        return _error(404, str(exc))
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

@workspace_bp.route("/conversations/<conversation_id>/participants", methods=["GET"])
def list_participants_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    return jsonify(ok=True, participants=workspace.list_participants(conversation_id))


@workspace_bp.route("/conversations/<conversation_id>/participants", methods=["POST"])
@limiter.limit("20 per minute")
def add_participant_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    identifier = str(data.get("identifier", ""))[:200].strip()
    if not identifier:
        return _error(400, "identifier requis.")
    target = database.get_user_by_identifier(identifier)
    if not target:
        return _error(404, "Utilisateur introuvable.")
    try:
        participants = workspace.add_participant(conversation_id, user["id"], target["id"])
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(404, str(exc))
    return jsonify(ok=True, participants=participants)


@workspace_bp.route("/conversations/<conversation_id>/participants/<target_user_id>", methods=["DELETE"])
def remove_participant_route(conversation_id, target_user_id):
    user, err = _require_user()
    if err:
        return err
    try:
        participants = workspace.remove_participant(conversation_id, user["id"], target_user_id)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    return jsonify(ok=True, participants=participants)


@workspace_bp.route("/conversations/<conversation_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_conversation_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", ""))[:200]
    try:
        conversation = workspace.rename_conversation(conversation_id, user["id"], title)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, conversation=conversation)


@workspace_bp.route("/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    try:
        deleted = workspace.delete_conversation(conversation_id, user["id"])
    except PermissionError as exc:
        return _error(403, str(exc))
    if not deleted:
        return _error(404, "Conversation introuvable.")
    return jsonify(ok=True)


@workspace_bp.route("/projects/<project_id>", methods=["DELETE"])
def delete_project_route(project_id):
    user, err = _require_user()
    if err:
        return err
    try:
        deleted = workspace.delete_project(project_id, user["id"])
    except PermissionError as exc:
        return _error(403, str(exc))
    if not deleted:
        return _error(404, "Projet introuvable.")
    return jsonify(ok=True)


@workspace_bp.route("/projects/<project_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_project_route(project_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))[:200]
    description = data.get("description")
    if description is not None:
        description = str(description)[:MAX_PROJECT_DESCRIPTION_INPUT_LENGTH]
    try:
        project = workspace.rename_project(project_id, user["id"], name, description=description)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, project=project)


# ---------------------------------------------------------------------------
# Temps reel : SSE (nouveaux messages) + typing (ephemere, jamais persiste)
# ---------------------------------------------------------------------------

def _sse_frame(event_id, event_type: str, data: dict) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


@workspace_bp.route("/events", methods=["GET"])
def events_route():
    user, err = _require_user()
    if err:
        return err
    conversation_id = request.args.get("conversationId", "")
    if not conversation_id or not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")

    # EventSource renvoie automatiquement Last-Event-Id a la reconnexion ;
    # ?after=N ne sert qu'au tout premier appel (pas encore de reconnexion).
    raw_last_id = request.headers.get("Last-Event-ID") or request.args.get("after", "0")
    try:
        after_seq = int(raw_last_id)
    except (TypeError, ValueError):
        after_seq = 0

    def generate():
        # S'abonner AVANT de lire le rattrapage Postgres : un message publie
        # pendant la requete de rattrapage est ainsi mis en tampon par Redis
        # plutot que perdu (au pire il sera vu deux fois, filtre ci-dessous
        # via last_seq - jamais perdu).
        pubsub = realtime.subscribe(conversation_id)
        last_seq = after_seq
        try:
            for message in workspace.list_messages_after(conversation_id, after_seq):
                last_seq = max(last_seq, message["seq"] or 0)
                yield _sse_frame(message["seq"], "message.created", message)
            while True:
                raw = pubsub.get_message(timeout=20, ignore_subscribe_messages=True)
                if raw is None:
                    yield ": heartbeat\n\n"
                    continue
                try:
                    payload = json.loads(raw["data"])
                except (TypeError, ValueError, KeyError):
                    continue
                event_type = payload.get("type", "message")
                data = payload.get("data", {})
                if event_type == "message.created":
                    seq = data.get("seq") or 0
                    if seq <= last_seq:
                        continue  # deja envoye pendant le rattrapage
                    last_seq = seq
                    yield _sse_frame(seq, event_type, data)
                else:
                    # typing et futurs evenements ephemeres : jamais rejoues
                    # au rattrapage, pas d'id SSE (rien a "reconnecter").
                    yield _sse_frame(None, event_type, data)
        finally:
            pubsub.close()

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@workspace_bp.route("/conversations/<conversation_id>/typing", methods=["POST"])
@limiter.limit("60 per minute")
def typing_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    try:
        realtime.publish_event(
            conversation_id,
            "typing",
            {"userId": user["id"], "displayName": user["displayName"], "avatarUrl": user["avatarUrl"]},
        )
    except Exception:
        pass  # ephemere et best-effort : ne jamais faire echouer cet appel
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Actions autorisees
# ---------------------------------------------------------------------------

@workspace_bp.route("/actions", methods=["GET"])
def list_actions_route():
    _user, err = _require_user()
    if err:
        return err
    return jsonify(ok=True, actions=sorted(ALLOWED_ACTIONS))


# ---------------------------------------------------------------------------
# Banque de boutons/actions (Phase 5) : persistee dans Postgres-jg_R via
# librairies/workflow_bank.py, jamais dans la base principale ni cote client.
#
# Note d'architecture (deviation assumee par rapport a une lecture littérale
# de la demande) : le "context_id" auquel une entry_action est rattachee est
# ici l'utilisateur lui-meme (user["id"]), pas la conversation. Une nouvelle
# discussion n'a pas encore d'id de conversation tant qu'aucun message n'a
# ete envoye (voir sendMessage() cote frontend) : il n'existe donc pas de
# conteneur stable auquel rattacher des boutons avant ce premier message. Le
# choix le plus proche de la demande ("les boutons que JE veux voir sous MON
# entry") est une bibliotheque personnelle par utilisateur, valable sur
# toutes ses conversations. A adapter si un rattachement par conversation
# est explicitement souhaite une fois qu'une conversation existe deja.
# ---------------------------------------------------------------------------

@workspace_bp.route("/action-bank", methods=["GET"])
def list_action_bank_route():
    user, err = _require_user()
    if err:
        return err
    search = str(request.args.get("search", ""))[:200]
    try:
        actions = workflow_bank.list_actions(search=search)
    except RuntimeError:
        return _error(503, "Banque de boutons non configuree.")
    return jsonify(ok=True, actions=_with_creator_names(actions))


@workspace_bp.route("/action-bank", methods=["POST"])
@limiter.limit("20 per minute")
def create_action_bank_route():
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:100]
    if not name:
        return _error(400, "Nom du bouton requis.")
    description = data.get("description")
    if description is not None:
        description = str(description).strip()[:2000] or None
    instruction = data.get("instruction")
    if instruction is not None:
        instruction = str(instruction).strip()[:20000] or None
    required_integrations = data.get("requiredIntegrations")
    if required_integrations is not None and not isinstance(required_integrations, list):
        return _error(400, "requiredIntegrations invalide.")
    required_files = data.get("requiredFiles")
    if required_files is not None and not isinstance(required_files, list):
        return _error(400, "requiredFiles invalide.")

    try:
        n8n_info = n8n_client.create_action_workflow(name)
    except N8nConfigError:
        return _error(503, "Integration n8n non configuree (N8N_API_URL/N8N_API_KEY).")
    except requests.exceptions.RequestException:
        return _error(502, "Impossible de creer le workflow n8n pour ce bouton.")

    try:
        workflow_record = workflow_bank.create_workflow_record(
            name=name,
            n8n_workflow_id=n8n_info["n8nWorkflowId"],
            webhook_path=n8n_info["webhookPath"],
            status="active" if n8n_info["active"] else "draft",
            editor_url=n8n_info["editorUrl"],
        )
        action = workflow_bank.create_action(
            name=name,
            created_by=user["id"],
            workflow_record_id=workflow_record["id"],
            description=description,
            instruction=instruction,
            required_integrations=required_integrations,
            required_files=required_files,
        )
    except RuntimeError:
        return _error(503, "Banque de boutons non configuree.")

    # Le createur est forcement l'utilisateur courant ici : pas besoin d'une
    # relecture base, on a deja son nom affichable sous la main.
    action["createdByName"] = user["displayName"]
    return jsonify(ok=True, action=action, workflowRecord=workflow_record)


@workspace_bp.route("/action-bank/<action_id>", methods=["PATCH"])
@limiter.limit("30 per minute")
def update_action_bank_route(action_id):
    """Edition partagee (nom/description/instruction/integrations/fichiers
    requis) avec verrouillage optimiste : le client doit renvoyer la
    `version` qu'il a lue en dernier ; en cas de conflit (quelqu'un d'autre a
    modifie entre-temps), renvoie 409 + l'action actuelle a jour, jamais un
    ecrasement silencieux (mission "boutons SPS" §12)."""
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if "version" not in data:
        return _error(400, "version requise (verrouillage optimiste).")
    try:
        expected_version = int(data.get("version"))
    except (TypeError, ValueError):
        return _error(400, "version invalide.")

    name = data.get("name")
    if name is not None:
        name = str(name).strip()[:100] or None
    description = data.get("description")
    if description is not None:
        description = str(description).strip()[:2000] or None
    instruction = data.get("instruction")
    if instruction is not None:
        instruction = str(instruction).strip()[:20000] or None
    required_integrations = data.get("requiredIntegrations")
    if required_integrations is not None and not isinstance(required_integrations, list):
        return _error(400, "requiredIntegrations invalide.")
    required_files = data.get("requiredFiles")
    if required_files is not None and not isinstance(required_files, list):
        return _error(400, "requiredFiles invalide.")

    try:
        updated = workflow_bank.update_action_details(
            action_id,
            actor_user_id=user["id"],
            expected_version=expected_version,
            name=name,
            description=description,
            instruction=instruction,
            required_integrations=required_integrations,
            required_files=required_files,
        )
    except LookupError as exc:
        return _error(404, str(exc))
    except workflow_bank.VersionConflictError as exc:
        current = workflow_bank.get_action(action_id)
        return jsonify(ok=False, error=str(exc), current=_with_creator_name(current) if current else current), 409
    except RuntimeError:
        return _error(503, "Banque de boutons non configuree.")
    return jsonify(ok=True, action=_with_creator_name(updated))


@workspace_bp.route("/action-bank/<action_id>", methods=["DELETE"])
def delete_action_bank_route(action_id):
    user, err = _require_user()
    if err:
        return err
    action = workflow_bank.get_action(action_id)
    if not action:
        return _error(404, "Action introuvable.")
    if action["createdBy"] != user["id"] and user.get("role") != "admin":
        return _error(403, "Seul le createur ou un administrateur peut supprimer definitivement cette action.")
    deleted = workflow_bank.delete_action(action_id)
    if not deleted:
        return _error(409, "Cette action est encore utilisee ailleurs : impossible de la supprimer definitivement.")

    # Nettoyage best-effort du workflow n8n associe : sans ca, l'action
    # disparaissait de la banque mais son workflow n8n restait actif et
    # joignable indefiniment, et sa ligne workflow_records restait orpheline
    # dans Postgres-jg_R (trouve lors de la validation E2E de la suppression
    # d'un bouton). Ne doit jamais faire echouer la suppression deja actee
    # du bouton cote base si n8n est indisponible.
    workflow_record_id = action.get("workflowRecordId")
    if workflow_record_id:
        n8n_workflow_id = None
        try:
            n8n_workflow_id = workflow_bank.delete_workflow_record(workflow_record_id)
        except RuntimeError:
            pass
        if n8n_workflow_id:
            try:
                n8n_client.delete_workflow(n8n_workflow_id)
            except (N8nConfigError, requests.exceptions.RequestException):
                pass
    return jsonify(ok=True)


@workspace_bp.route("/entry-actions", methods=["GET"])
def list_entry_actions_route():
    user, err = _require_user()
    if err:
        return err
    try:
        entry_actions = workflow_bank.list_entry_actions(context_id=SHARED_BUTTON_CONTEXT_ID)
    except RuntimeError:
        return _error(503, "Banque de boutons non configuree.")
    return jsonify(ok=True, entryActions=_with_entry_action_creator_names(entry_actions))


@workspace_bp.route("/entry-actions", methods=["POST"])
@limiter.limit("30 per minute")
def add_entry_action_route():
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    action_id = str(data.get("actionId", ""))
    alias = data.get("alias")
    if alias is not None:
        alias = str(alias).strip()[:100] or None

    action = workflow_bank.get_action(action_id)
    if not action:
        return _error(404, "Action introuvable.")

    entry_action = workflow_bank.add_entry_action(
        context_id=SHARED_BUTTON_CONTEXT_ID, action_id=action_id, created_by=user["id"], alias=alias
    )
    return jsonify(ok=True, entryAction=_with_entry_action_creator_names([entry_action])[0])


@workspace_bp.route("/entry-actions/<entry_action_id>", methods=["PATCH"])
def update_entry_action_route(entry_action_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if "alias" not in data:
        return _error(400, "Rien a mettre a jour.")
    alias = data.get("alias")
    alias = str(alias).strip()[:100] if alias else None
    ok = workflow_bank.rename_entry_action_alias(entry_action_id, context_id=SHARED_BUTTON_CONTEXT_ID, alias=alias)
    if not ok:
        return _error(404, "Bouton introuvable.")
    return jsonify(ok=True)


@workspace_bp.route("/entry-actions/<entry_action_id>", methods=["DELETE"])
def remove_entry_action_route(entry_action_id):
    user, err = _require_user()
    if err:
        return err
    ok = workflow_bank.remove_entry_action(entry_action_id, context_id=SHARED_BUTTON_CONTEXT_ID)
    if not ok:
        return _error(404, "Bouton introuvable.")
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Banque de connexions plateformes/API : bouton "Plateformes/API" de
# l'entry (a gauche du trombone). Meme principe de banque centrale et
# partagee que la banque de boutons ci-dessus (voir la note d'architecture
# dans librairies/connections_bank.py). La clef API en clair n'est JAMAIS
# renvoyee par aucune de ces routes (voir _public_connection).
# ---------------------------------------------------------------------------

MAX_CONNECTIONS_PER_MESSAGE = 10


@workspace_bp.route("/connections", methods=["GET"])
def list_connections_route():
    _user, err = _require_user()
    if err:
        return err
    search = str(request.args.get("search", ""))[:200]
    try:
        connections = connections_bank.list_connections(search=search)
    except RuntimeError:
        return _error(503, "Banque de connexions non configuree.")
    # Jamais un bouton "Se connecter (OAuth)" qui echouerait systematiquement
    # (meme principe que google_drive_status_route/`configured`) : le
    # frontend ne le propose que si CONNECTIONS_OAUTH_REDIRECT_URI est bien
    # configuree sur ce deploiement.
    return jsonify(ok=True, connections=connections, oauthGloballyConfigured=oauth_connector.is_globally_configured())


@workspace_bp.route("/connections", methods=["POST"])
@limiter.limit("20 per minute")
def create_connection_route():
    """Seul le nom est obligatoire : les DEUX entrees possibles ci-dessous
    (cle API generalisee / OAuth generique, voir librairies/connections_bank.py)
    sont facultatives et independantes -- on peut remplir l'une, l'autre, les
    deux, ou aucune (la connexion reste alors juste un espace reserve, sans
    credential, jusqu'a une modification ulterieure)."""
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:100]
    keywords = data.get("keywords") or []
    if not name:
        return _error(400, "Nom de la connexion requis.")
    if not isinstance(keywords, list):
        return _error(400, "Mots-cles invalides.")

    api_entry = data.get("apiKeyEntry") or {}
    if not isinstance(api_entry, dict):
        return _error(400, "Entree cle API invalide.")
    api_key = str(api_entry.get("apiKey", "")).strip()
    base_url = str(api_entry.get("baseUrl", "")).strip()
    auth_location = str(api_entry.get("authLocation", "header_bearer")).strip() or "header_bearer"
    auth_field_name = str(api_entry.get("authFieldName", "")).strip()[:100]
    if auth_location not in connections_bank.AUTH_LOCATIONS:
        return _error(400, "Type d'authentification invalide.")
    if auth_location != "header_bearer" and not auth_field_name:
        return _error(400, "Nom du champ requis pour ce type d'authentification.")

    oauth_entry = data.get("oauthEntry") or {}
    if not isinstance(oauth_entry, dict):
        return _error(400, "Entree OAuth invalide.")
    oauth_client_id = str(oauth_entry.get("clientId", "")).strip()
    oauth_client_secret = str(oauth_entry.get("clientSecret", "")).strip()
    oauth_authorize_url = str(oauth_entry.get("authorizeUrl", "")).strip()
    oauth_token_url = str(oauth_entry.get("tokenUrl", "")).strip()
    oauth_scope = str(oauth_entry.get("scope", "")).strip()
    oauth_fields = (oauth_client_id, oauth_client_secret, oauth_authorize_url, oauth_token_url)
    # Independantes entre elles (entree 1 vs entree 2), mais coherente EN
    # INTERNE : un flux OAuth partiel (2 champs sur 4) ne peut jamais
    # fonctionner, donc soit tous les 4 champs, soit aucun.
    if any(oauth_fields) and not all(oauth_fields):
        return _error(400, "Renseigne les 4 champs de connexion OAuth (client, secret, URL d'autorisation, URL de jeton), ou aucun.")

    try:
        connection = connections_bank.create_connection(
            name=name,
            created_by=user["id"],
            keywords=keywords,
            api_key=api_key,
            oauth_client_secret=oauth_client_secret,
            config={
                "baseUrl": base_url,
                "authLocation": auth_location,
                "authFieldName": auth_field_name,
                "oauthClientId": oauth_client_id,
                "oauthAuthorizeUrl": oauth_authorize_url,
                "oauthTokenUrl": oauth_token_url,
                "oauthScope": oauth_scope,
            },
        )
    except RuntimeError:
        return _error(503, "Banque de connexions non configuree.")
    return jsonify(ok=True, connection=connection)


@workspace_bp.route("/connections/<connection_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_connection_route(connection_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:100]
    if not name:
        return _error(400, "Nom requis.")
    try:
        connection = connections_bank.rename_connection(
            connection_id, name, actor_user_id=user["id"], is_admin=user.get("role") == "admin"
        )
    except PermissionError as exc:
        return _error(403, str(exc))
    except RuntimeError:
        return _error(503, "Banque de connexions non configuree.")
    if not connection:
        return _error(404, "Connexion introuvable.")
    return jsonify(ok=True, connection=connection)


@workspace_bp.route("/connections/<connection_id>", methods=["DELETE"])
def delete_connection_route(connection_id):
    user, err = _require_user()
    if err:
        return err
    try:
        deleted = connections_bank.delete_connection(
            connection_id, actor_user_id=user["id"], is_admin=user.get("role") == "admin"
        )
    except PermissionError as exc:
        return _error(403, str(exc))
    except RuntimeError:
        return _error(503, "Banque de connexions non configuree.")
    if not deleted:
        return _error(404, "Connexion introuvable.")
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Entree 2 (OAuth generique) d'une connexion de la banque, voir
# librairies/oauth_connector.py. Contrairement a Google Drive ci-dessous, le
# `state` doit vehiculer QUELLE connexion est en cours d'autorisation (une
# seule route de callback, partagee par toutes les plateformes externes
# possibles) : on l'encode en clair dans le state lui-meme (pas un secret,
# juste un identifiant), le cookie sert uniquement a verifier qu'il vient
# bien de CE navigateur (protection CSRF standard du flux OAuth).
# ---------------------------------------------------------------------------

def _build_connections_oauth_state(connection_id: str) -> str:
    return f"{connection_id}.{generate_token(16)}"


def _connection_id_from_state(state: str) -> str | None:
    if not state or "." not in state:
        return None
    connection_id, _, _ = state.partition(".")
    return connection_id or None


@workspace_bp.route("/connections/<connection_id>/oauth/connect", methods=["GET"])
@limiter.limit("10 per minute")
def connect_connection_oauth_route(connection_id):
    user, err = _require_user()
    if err:
        return err
    if not oauth_connector.is_globally_configured():
        return _error(503, "Connecteur OAuth non configure sur ce deploiement.")
    try:
        state = _build_connections_oauth_state(connection_id)
        authorization_url = oauth_connector.build_authorization_url(connection_id, state)
    except OAuthConnectorConfigError:
        return _error(400, "Cette connexion n'a pas de configuration OAuth (entree 2 vide).")
    response = redirect(authorization_url)
    response.set_cookie(
        CONNECTIONS_OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "true").lower() != "false",
        samesite="Lax",
        path="/",
    )
    return response


@workspace_bp.route("/connections/oauth/callback", methods=["GET"])
def connections_oauth_callback_route():
    user, err = _require_user()
    if err:
        return err
    frontend_url = os.environ.get("FRONTEND_URL", "").rstrip("/")
    redirect_target = f"{frontend_url}/page2.html" if frontend_url else "/page2.html"

    def _redirect_with_status(status: str):
        response = redirect(f"{redirect_target}?connectionsOAuthStatus={status}")
        response.delete_cookie(CONNECTIONS_OAUTH_STATE_COOKIE, path="/")
        return response

    expected_state = request.cookies.get(CONNECTIONS_OAUTH_STATE_COOKIE)
    received_state = request.args.get("state")
    if not expected_state or not received_state or not hmac.compare_digest(expected_state, received_state):
        return _redirect_with_status("state_mismatch")
    connection_id = _connection_id_from_state(received_state)
    code = request.args.get("code")
    if not connection_id or not code:
        return _redirect_with_status("denied")
    try:
        oauth_connector.exchange_code_and_store(connection_id, code)
    except (OAuthConnectorConfigError, OAuthConnectorError):
        return _redirect_with_status("error")
    return _redirect_with_status("connected")


@workspace_bp.route("/connections/<connection_id>/oauth", methods=["DELETE"])
def disconnect_connection_oauth_route(connection_id):
    user, err = _require_user()
    if err:
        return err
    if not connections_bank.get_connection(connection_id):
        return _error(404, "Connexion introuvable.")
    oauth_connector.disconnect(connection_id)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Integration Google Drive (bouton "Resume Drive", mission §5) : OAuth
# personnel par utilisateur (voir librairies/google_drive.py) -- contrairement
# a la banque de connexions ci-dessus, ce jeton n'est jamais partage.
# ---------------------------------------------------------------------------

@workspace_bp.route("/integrations/google-drive/status", methods=["GET"])
def google_drive_status_route():
    user, err = _require_user()
    if err:
        return err
    status = google_drive.get_connection_status(user["id"])
    status["configured"] = google_drive.is_configured()
    return jsonify(ok=True, **status)


@workspace_bp.route("/integrations/google-drive/connect", methods=["GET"])
@limiter.limit("10 per minute")
def google_drive_connect_route():
    user, err = _require_user()
    if err:
        return err
    if not google_drive.is_configured():
        return _error(503, "Integration Google Drive non configuree.")
    state = generate_token(16)
    response = redirect(google_drive.build_authorization_url(state))
    response.set_cookie(
        GOOGLE_OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "true").lower() != "false",
        samesite="Lax",
        path="/",
    )
    return response


@workspace_bp.route("/integrations/google-drive/callback", methods=["GET"])
def google_drive_callback_route():
    user, err = _require_user()
    if err:
        return err
    frontend_url = os.environ.get("FRONTEND_URL", "").rstrip("/")
    redirect_target = f"{frontend_url}/page2.html" if frontend_url else "/page2.html"

    def _redirect_with_status(status: str):
        response = redirect(f"{redirect_target}?googleDriveStatus={status}")
        response.delete_cookie(GOOGLE_OAUTH_STATE_COOKIE, path="/")
        return response

    expected_state = request.cookies.get(GOOGLE_OAUTH_STATE_COOKIE)
    received_state = request.args.get("state")
    if not expected_state or not received_state or not hmac.compare_digest(expected_state, received_state):
        return _redirect_with_status("state_mismatch")
    code = request.args.get("code")
    if not code:
        return _redirect_with_status("denied")
    try:
        google_drive.connect_user(user["id"], code)
    except (GoogleDriveConfigError, GoogleDriveError):
        return _redirect_with_status("error")
    return _redirect_with_status("connected")


@workspace_bp.route("/integrations/google-drive", methods=["DELETE"])
def google_drive_disconnect_route():
    user, err = _require_user()
    if err:
        return err
    google_drive.disconnect_user(user["id"])
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Resultats reutilisables
# ---------------------------------------------------------------------------

@workspace_bp.route("/results/<result_id>", methods=["GET"])
def get_result_route(result_id):
    user, err = _require_user()
    if err:
        return err
    result = workspace.get_result(result_id)
    if not result:
        return _error(404, "Resultat introuvable.")
    # Trouve pendant la validation E2E : cette route ne verifiait jusqu'ici
    # que l'authentification, jamais l'appartenance a la conversation -- un
    # utilisateur authentifie quelconque pouvait lire le resultat COMPLET
    # (prompt, donnees plateformes/API recuperees, etc.) de N'IMPORTE QUELLE
    # conversation d'un autre utilisateur en devinant/observant son
    # result_id. Meme traitement (404, pas 403) que get_conversation_route :
    # ne pas reveler qu'une conversation/un resultat existe a quelqu'un qui
    # n'y a pas acces.
    if not workspace.is_participant(result["conversationId"], user["id"]):
        return _error(404, "Resultat introuvable.")
    return jsonify(ok=True, result=result)


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------

@workspace_bp.route("/files", methods=["POST"])
@limiter.limit("30 per minute")
def upload_file_route():
    user, err = _require_user()
    if err:
        return err

    if "file" not in request.files:
        return _error(400, "Aucun fichier recu.")
    uploaded = request.files["file"]
    if not uploaded.filename:
        return _error(400, "Nom de fichier manquant.")

    mime_type = uploaded.mimetype or mimetypes.guess_type(uploaded.filename)[0] or "application/octet-stream"
    if mime_type not in ALLOWED_MIME_TYPES:
        return _error(415, "Type de fichier non autorise.")

    content = uploaded.read()
    if not content:
        return _error(400, "Fichier vide.")
    if len(content) > MAX_UPLOAD_BYTES:
        return _error(413, "Fichier trop volumineux.")

    file_id = workspace.new_id("file")
    storage_name = _safe_storage_name(file_id, uploaded.filename)
    storage_path = os.path.join(UPLOAD_DIR, storage_name)
    with open(storage_path, "wb") as handle:
        handle.write(content)

    record = workspace.create_file_record(
        file_id=file_id,
        user_id=user["id"],
        original_name=uploaded.filename[:200],
        mime_type=mime_type,
        size_bytes=len(content),
        storage_reference=storage_name,
    )
    return jsonify(ok=True, file=record)


@workspace_bp.route("/files/<file_id>", methods=["GET"])
def download_file_route(file_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.user_can_access_file(file_id, user["id"]):
        return _error(403, "Acces refuse.")
    return _serve_file(file_id)


@workspace_bp.route("/files/<file_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_file_route(file_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))[:200]
    try:
        file = workspace.rename_file(file_id, user["id"], name)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, file=file)


@workspace_bp.route("/files/<file_id>/signed", methods=["GET"])
def download_file_signed_route(file_id):
    """Utilise par n8n pour recuperer un document : lien temporaire signe
    (HMAC + expiration courte) plutot qu'une URL publique permanente, car
    n8n n'a pas de session navigateur/cookie."""
    expires_at = request.args.get("exp", type=int)
    token = request.args.get("token", "")
    if not expires_at or not token:
        return _error(400, "Lien invalide.")
    if int(time.time()) > expires_at:
        return _error(410, "Lien expire.")
    try:
        expected = sign_file_token(file_id, expires_at)
    except RuntimeError:
        return _error(503, "Signature de fichiers non configuree.")
    if not hmac.compare_digest(expected, token):
        return _error(403, "Lien invalide.")
    return _serve_file(file_id)


# ---------------------------------------------------------------------------
# Envoi d'un message -> conversation -> n8n -> rich_response
# ---------------------------------------------------------------------------

@workspace_bp.route("/messages", methods=["POST"])
@limiter.limit("20 per minute")
def send_message_route():
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversationId") or None
    project_id = data.get("projectId") or None
    model = str(data.get("model", ""))
    message_text = str(data.get("message", ""))[:8000].strip()
    file_ids = data.get("fileIds") or []
    source_result_ids = data.get("sourceResultIds") or []
    connection_ids = data.get("connectionIds") or []
    action = data.get("action") or None
    reply_to_message_id = str(data.get("replyToMessageId") or "") or None
    request_id = str(data.get("requestId") or "")[:100] or str(uuid.uuid4())
    is_message_mode = model == MESSAGE_MODE

    if model not in ALLOWED_MODELS and not is_message_mode:
        return _error(400, "Modele invalide.")
    if not message_text and not file_ids:
        return _error(400, "Message vide.")
    if not isinstance(file_ids, list) or len(file_ids) > MAX_FILES_PER_MESSAGE:
        return _error(400, "Nombre de fichiers invalide.")
    if not isinstance(source_result_ids, list):
        return _error(400, "sourceResultIds invalide.")
    if not isinstance(connection_ids, list) or len(connection_ids) > MAX_CONNECTIONS_PER_MESSAGE:
        return _error(400, "Nombre de connexions invalide.")

    action_id = None
    action_parameters = {}
    # Mode "Message" (mission §4) : jamais d'IA ni de bouton/workflow, un
    # bouton/action selectionne par ailleurs dans l'entry est donc ignore ici
    # plutot que rejete (comportement le moins surprenant si l'utilisateur
    # change de mode sans reinitialiser sa selection de bouton).
    if action and not is_message_mode:
        action_id = str(action.get("id", ""))
        # Deux familles d'actions coexistent : l'ancienne liste statique
        # ALLOWED_ACTIONS (jamais reellement branchee, id toujours null cote
        # frontend jusqu'ici) et la nouvelle banque dynamique persistee dans
        # Postgres-jg_R (Phase 5, qui inclut aussi le bouton integre "Resume
        # Drive", voir workflow_bank.GOOGLE_DRIVE_ACTION_ID). On accepte l'une
        # ou l'autre pour ne rien casser si l'ancienne liste venait a etre
        # utilisee ailleurs.
        if action_id not in ALLOWED_ACTIONS and not workflow_bank.get_action(action_id):
            return _error(400, "Action non autorisee.")
        action_parameters = action.get("parameters") or {}

    for file_id in file_ids:
        if not workspace.user_can_access_file(file_id, user["id"]):
            return _error(403, "Acces refuse a un fichier.")
    for result_id in source_result_ids:
        source_result = workspace.get_result(result_id)
        # Meme faille corrigee sur get_result_route ci-dessus, appliquee ici :
        # une simple existence ne suffit pas, sinon n'importe quel
        # utilisateur pourrait enchainer le resultat prive d'une conversation
        # d'un autre utilisateur comme "source" de son propre message.
        if not source_result or not workspace.is_participant(source_result["conversationId"], user["id"]):
            return _error(404, "Resultat source introuvable.")
    for connection_id in connection_ids:
        # Banque partagee (comme les boutons) : n'importe quel utilisateur
        # authentifie peut utiliser une connexion existante, mais un
        # connectionId invente/obsolete cote client est toujours rejete ici
        # (jamais fait confiance a une configuration venue du frontend, §36/44).
        if not connections_bank.get_connection(connection_id):
            return _error(404, "Connexion introuvable.")
    if reply_to_message_id:
        # "Repondre" (mission §2/§3) : le message cible doit exister ET
        # appartenir a une conversation ou l'utilisateur est deja participant
        # -- jamais une simple existence (meme faille de principe que pour
        # source_result_ids/result_id ci-dessus), et jamais une conversation
        # differente de celle ou le nouveau message est envoye.
        target_message = workspace.get_message(reply_to_message_id)
        if (
            not target_message
            or not conversation_id
            or target_message["conversationId"] != conversation_id
            or not workspace.is_participant(conversation_id, user["id"])
        ):
            return _error(404, "Message cible introuvable.")

    # Idempotence : un meme requestId rejoue (retry reseau, double-clic) ne
    # redeclenche jamais un second workflow ni un second message utilisateur.
    existing_run = workspace.get_workflow_run_by_request_id(request_id)
    if existing_run:
        if not workspace.is_participant(existing_run["conversationId"], user["id"]):
            return _error(404, "Conversation introuvable.")
        if existing_run["status"] == "completed" and existing_run["resultId"]:
            result = workspace.get_result(existing_run["resultId"])
            return jsonify(
                ok=True,
                conversationId=existing_run["conversationId"],
                resultId=result["id"],
                blocks=result["response"].get("blocks", []),
                replay=True,
            )
        if existing_run["status"] == "running":
            return _error(409, "Cette requete est deja en cours de traitement.")

        # status 'failed' ou 'timeout' : on retente sur le MEME run et le
        # MEME message utilisateur (deja enregistres lors de la 1re tentative),
        # sans rien dupliquer en base.
        run_id = workspace.retry_workflow_run(request_id)
        if run_id is None:
            return _error(409, "Cette requete est deja en cours de traitement.")
        conversation_id = existing_run["conversationId"]
        conversation = workspace.get_conversation(conversation_id)
        user_message = workspace.get_message(existing_run["messageId"]) if existing_run["messageId"] else None
        is_new_conversation = False
    else:
        is_new_conversation = not conversation_id
        if is_new_conversation:
            try:
                conversation = workspace.create_conversation(
                    user["id"], user["displayName"], workspace.derive_title(message_text), project_id
                )
            except ValueError as exc:
                return _error(404, str(exc))
            conversation_id = conversation["id"]
        else:
            if not workspace.is_participant(conversation_id, user["id"]):
                return _error(404, "Conversation introuvable.")
            conversation = workspace.get_conversation(conversation_id)
            if not conversation:
                return _error(404, "Conversation introuvable.")

        user_message = workspace.add_message(
            conversation_id=conversation_id,
            user_id=user["id"],
            author_name=user["displayName"],
            role="user",
            content=message_text,
            model=model,
            action_id=action_id,
            reply_to_message_id=reply_to_message_id,
        )
        if file_ids:
            workspace.link_files_to_message(user_message["id"], file_ids)
        workspace.touch_conversation(conversation_id)

        if is_message_mode:
            # Mode "Message" (mission §4) : le texte est deja enregistre et
            # diffuse (message.created, voir workspace.add_message) comme
            # n'importe quel message -- aucun run, aucune mise en file,
            # jamais d'appel IA. Il redevient disponible comme contexte pour
            # ChatGPT/Claude via le prochain message de la conversation
            # (voir librairies/jobs.py::_build_conversation_history_context).
            return jsonify(
                ok=True,
                conversationId=conversation_id,
                isNewConversation=is_new_conversation,
                conversationTitle=conversation["title"],
                userMessage=user_message,
                requestId=request_id,
                queued=False,
            )

        run = workspace.start_workflow_run(conversation_id, user_message["id"], user["id"], model, request_id)
        if run is None:
            return _error(409, "Cette requete est deja en cours de traitement.")
        run_id = run["id"]

    # Phase 4 : l'appel n8n (potentiellement lent, jusqu'a N8N_TIMEOUT_SECONDS)
    # ne se fait plus ici. On met en file et on repond tout de suite ; le
    # worker (librairies/jobs.py) persiste le resultat et pousse la reponse
    # via SSE (voir realtime.py) quand elle est prete.
    queue.enqueue(
        execute_workflow_run,
        run_id,
        conversation_id,
        user_message["id"],
        user["id"],
        user["displayName"],
        model,
        action_id,
        action_parameters,
        message_text,
        file_ids,
        source_result_ids,
        connection_ids,
        request_id,
        reply_to_message_id,
        job_timeout=N8N_TIMEOUT_SECONDS + 30,
    )

    return jsonify(
        ok=True,
        conversationId=conversation_id,
        isNewConversation=is_new_conversation,
        conversationTitle=conversation["title"],
        userMessage=user_message,
        requestId=request_id,
        queued=True,
    )
