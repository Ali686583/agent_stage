"""
librairies/jobs.py
====================

Traitement asynchrone des demandes IA/n8n (Phase 4). Le web (workspace_routes.py)
se contente d'enregistrer le message utilisateur et de mettre ce travail en
file d'attente Redis (RQ) ; c'est CE module, execute par un worker separe
(voir worker.py et le service Railway "worker"), qui appelle reellement n8n.

Objectif : qu'un appel n8n lent (jusqu'a N8N_TIMEOUT_SECONDS) n'occupe
jamais un thread du serveur web, et ne bloque donc jamais les autres
utilisateurs (voir le prompt d'architecture qui a demande cette phase).

Ce module doit rester importable SANS contexte de requete Flask (le worker
RQ n'en a pas) : jamais de `flask.request` ici, seulement des arguments
explicites et des variables d'environnement (FRONTEND_URL a la place de
request.url_root).
"""

from __future__ import annotations

import os
import time

import redis
import requests
from rq import Queue

from librairies import connections_bank, google_drive, platform_client, web_search, workflow_bank, workspace
from librairies.google_drive import GoogleDriveConfigError, GoogleDriveError
from librairies.security import sign_file_token
from librairies.web_search import WebSearchError

REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL n'est pas configuree : voir librairies/jobs.py.")

_redis = redis.Redis.from_url(REDIS_URL)
queue = Queue("agent_stage_workflows", connection=_redis)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
FILE_LINK_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

# Deux webhooks distincts (un par modele) : chacun peut etre construit comme
# un workflow n8n independant et simple, plutot qu'un seul workflow qui
# devrait brancher lui-meme sur le modele.
N8N_WEBHOOK_CHATGPT_URL = os.environ.get("N8N_WEBHOOK_CHATGPT_URL", "")
N8N_WEBHOOK_CLAUDE_URL = os.environ.get("N8N_WEBHOOK_CLAUDE_URL", "")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")
N8N_TIMEOUT_SECONDS = int(os.environ.get("N8N_TIMEOUT_SECONDS", "90"))
# Meme instance n8n que N8N_API_URL (utilisee cote backend pour creer les
# workflows de la banque, voir librairies/n8n_client.py) : sert ici a
# reconstruire l'URL d'execution reelle d'un bouton de la banque a partir de
# son webhook_path stocke dans Postgres-jg_R.
N8N_API_URL = os.environ.get("N8N_API_URL", "").rstrip("/")


def _extract_result_text(result: dict) -> str:
    """Texte lisible d'un resultat deja persiste (reponse assistant d'un
    tour precedent), utilise pour construire le contexte "Repondre a une
    reponse" ci-dessous. Meme logique que extractMessagePlainText() cote
    frontend (workspace.js), volontairement dupliquee en Python plutot que
    partagee : ce module ne peut pas importer du JS, et la logique est assez
    courte pour ne pas justifier un endpoint dedie."""
    blocks = ((result or {}).get("response") or {}).get("blocks") or []
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "table":
            header = " | ".join(str(h) for h in (block.get("headers") or []))
            rows = "\n".join(" | ".join(str(c) for c in row) for row in (block.get("rows") or []))
            joined = "\n".join(part for part in (header, rows) if part)
            if joined:
                parts.append(joined)
        else:
            content = block.get("content") or block.get("text") or block.get("label") or ""
            if content:
                parts.append(content)
    return "\n\n".join(parts)


# Budget de caracteres pour l'historique de conversation injecte dans le
# prompt (mission "contexte continu" §1) : assez genereux pour une vraie
# continuite, sans jamais envoyer une conversation entiere devenue enorme.
# Volontairement un compte de caracteres (pas de tokenizer disponible sans
# dependance supplementaire) : un ordre de grandeur suffisant, pas une limite
# exacte de tokens.
MAX_HISTORY_CONTEXT_CHARS = 6000
# Combien de messages RECENTS au maximum sont candidats a l'historique avant
# meme d'appliquer le budget de caracteres ci-dessus (evite de charger des
# milliers de messages depuis Postgres pour une tres longue conversation
# alors que seuls les plus recents seront de toute facon retenus).
HISTORY_CANDIDATE_LIMIT = 100


def _resolve_reply_context_texts(reply_to_message_id: str | None, source_result_ids: list[str]) -> list[str]:
    """Contenu(s) prioritaire(s) d'une reponse explicite (mission §2) :
    d'abord le message cible par `reply_to_message_id` (mecanisme generalise,
    fonctionne pour un message utilisateur, un "Message", ou une reponse
    IA/bouton), puis les `source_result_ids` historiques (mecanisme plus
    ancien, garde fonctionnel a l'identique pour ne rien casser)."""
    texts = []
    if reply_to_message_id:
        try:
            target_message = workspace.get_message(reply_to_message_id)
        except Exception:
            target_message = None
        if target_message:
            text = None
            if target_message.get("resultId"):
                try:
                    referenced_result = workspace.get_result(target_message["resultId"])
                except Exception:
                    referenced_result = None
                if referenced_result:
                    text = _extract_result_text(referenced_result)
            if not text:
                text = target_message.get("content")
            if text:
                texts.append(text)
    for result_id in source_result_ids or []:
        try:
            referenced_result = workspace.get_result(result_id)
        except Exception:
            referenced_result = None
        if not referenced_result:
            continue
        text = _extract_result_text(referenced_result)
        if text:
            texts.append(text)
    return texts


def _label_for_history_message(message: dict) -> str:
    if message.get("role") == "assistant":
        return message.get("authorName") or "Assistant"
    return message.get("authorName") or "Utilisateur"


def _build_conversation_history_context(conversation_id: str, exclude_message_id: str) -> str:
    """Contexte conversationnel "si besoin" (mission §1) : les tours recents
    de CETTE discussion, quelle que soit l'IA/le mode qui les a produits
    (ChatGPT, Claude, bouton/workflow, ou simple "Message" -- tous melanges
    dans la meme table messages, voir librairies/workspace.py). Strategie de
    budget : on garde les tours les PLUS RECENTS jusqu'a la limite de
    caracteres, jamais un message tronque au milieu (on ecarte des tours
    entiers, jamais une partie d'un tour) -- ne tronque donc jamais une
    information a l'interieur d'un message, seulement l'anciennete retenue."""
    try:
        messages = workspace.list_messages(conversation_id, limit=HISTORY_CANDIDATE_LIMIT)
    except Exception:
        return ""
    relevant = [m for m in messages if m["id"] != exclude_message_id and (m.get("content") or "").strip()]
    if not relevant:
        return ""
    kept = []
    total = 0
    for message in reversed(relevant):
        line = f"{_label_for_history_message(message)}: {message['content'].strip()}"
        if kept and total + len(line) > MAX_HISTORY_CONTEXT_CHARS:
            break
        kept.append(line)
        total += len(line)
    kept.reverse()
    return "\n\n".join(kept)


def _build_prompt_with_context(
    conversation_id: str,
    user_message_id: str,
    message_text: str,
    source_result_ids: list[str],
    reply_to_message_id: str | None,
) -> str:
    """Construit le prompt final envoye a n8n (champ unique lu par tous les
    workflows existants/futurs, voir le commentaire historique ci-dessous) :
    priorite a une reponse explicite ciblee (mission §2, "distinguer
    clairement" du cas general), sinon contexte conversationnel general "si
    besoin" (mission §1). Jamais les deux a la fois : un message cible de
    facon explicite EST deja le contexte pertinent, y ajouter tout
    l'historique general diluerait cette priorite explicitement demandee."""
    referenced_texts = _resolve_reply_context_texts(reply_to_message_id, source_result_ids)
    if referenced_texts:
        quoted = "\n\n---\n\n".join(referenced_texts)
        return (
            "L'utilisateur repond specifiquement au message precedent suivant "
            "(contexte prioritaire a prendre en compte, ne pas le reproduire tel quel) :\n"
            f"{quoted}\n\n"
            "Nouvelle demande de l'utilisateur, en reponse a ce message precis :\n"
            f"{message_text}"
        )
    history = _build_conversation_history_context(conversation_id, user_message_id)
    if not history:
        return message_text
    return (
        "Historique pertinent de cette discussion, a prendre en compte si "
        "necessaire pour comprendre la demande ci-dessous (ne pas le "
        "reproduire tel quel) :\n\n"
        f"{history}\n\n"
        "Nouvelle demande de l'utilisateur, dans la continuite de cette discussion :\n"
        f"{message_text}"
    )


def _fail(run_id: str, conversation_id: str, message_id: str, request_id: str, status: str, error: str) -> None:
    workspace.complete_workflow_run(run_id, status=status, error=error)
    try:
        workspace.publish_workflow_failed(conversation_id, request_id, message_id, error)
    except Exception:
        pass  # best-effort : le run reste correctement marque en base malgre tout


def _fetch_google_drive_document(user_id: str, message_text: str) -> str:
    """Recupere le contenu texte du document Drive reference dans la demande
    de l'utilisateur. Leve GoogleDriveError avec un code stable (mission §5,
    "gerer proprement" chaque cas d'echec) ; n'invente jamais de contenu."""
    access_token = google_drive.get_valid_access_token(user_id)
    if not access_token:
        raise GoogleDriveError("not_connected")
    file_id = google_drive.extract_drive_file_id(message_text)
    if not file_id:
        raise GoogleDriveError("no_reference_found")
    return google_drive.fetch_document_text(access_token, file_id)


def _build_web_monitoring_prompt(user_query: str, results_text: str) -> str:
    return (
        "Voici des resultats REELS de recherche sur des sources publiques pour "
        "la demande ci-dessous (titres, extraits, liens). N'invente aucune "
        "information au-dela de ce qui est fourni ici, et indique clairement si "
        "ces resultats sont insuffisants pour repondre completement.\n\n"
        f"--- RESULTATS DE RECHERCHE ---\n{results_text}\n--- FIN DES RESULTATS ---\n\n"
        f"Demande de l'utilisateur : {user_query}"
    )


def _build_drive_summary_prompt(user_instruction: str, document_text: str) -> str:
    instruction = (user_instruction or "").strip()
    extra = f"\n\nConsigne complementaire de l'utilisateur : {instruction}" if instruction else ""
    return (
        "Resume le document suivant de maniere structuree et fidele au contenu "
        "reel (titres, points cles, tableau si pertinent). N'invente aucune "
        "information absente du document ci-dessous.\n\n"
        f"--- DOCUMENT ---\n{document_text}\n--- FIN DU DOCUMENT ---{extra}"
    )


def execute_workflow_run(
    run_id: str,
    conversation_id: str,
    user_message_id: str,
    user_id: str,
    user_name: str,
    model: str,
    action_id: str | None,
    action_parameters: dict,
    message_text: str,
    file_ids: list[str],
    source_result_ids: list[str],
    connection_ids: list[str],
    request_id: str,
    reply_to_message_id: str | None = None,
) -> None:
    drive_document_text = None
    web_search_results_text = None
    # Bouton integre "Resume Drive" (mission §5) : jamais route vers un
    # webhook n8n comme les autres boutons de la banque (il n'en a pas, voir
    # workflow_bank.ensure_google_drive_action) -- le contenu Drive, une fois
    # recupere ici cote serveur, est simplement injecte dans le prompt envoye
    # au MEME webhook ChatGPT/Claude que la conversation normale (aucune
    # modification n8n necessaire, meme principe de reutilisation que le
    # contexte de reponse/historique ci-dessus).
    if action_id == workflow_bank.GOOGLE_DRIVE_ACTION_ID:
        webhook_url = N8N_WEBHOOK_CHATGPT_URL if model == "chatgpt" else N8N_WEBHOOK_CLAUDE_URL
        if not webhook_url:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "workflow_not_configured")
            return
        try:
            drive_document_text = _fetch_google_drive_document(user_id, message_text)
        except GoogleDriveConfigError:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "google_drive_not_configured")
            return
        except GoogleDriveError as exc:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", f"google_drive_{exc}")
            return
    # Boutons integres "Veille Web (sans API)" / "Veille Web (recherche
    # generale)" : meme principe que Resume Drive ci-dessus -- recherche
    # reelle cote serveur sur des sources PUBLIQUES uniquement, jamais de
    # donnee personnelle, jamais un resultat invente (voir
    # librairies/web_search.py). Aucune des deux ne necessite de cle API ni
    # de compte (Tavily a ete ecarte : il demandait une carte bancaire meme
    # sur son offre gratuite).
    elif action_id in (workflow_bank.WEB_MONITORING_FREE_ACTION_ID, workflow_bank.WEB_MONITORING_GENERAL_ACTION_ID):
        webhook_url = N8N_WEBHOOK_CHATGPT_URL if model == "chatgpt" else N8N_WEBHOOK_CLAUDE_URL
        if not webhook_url:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "workflow_not_configured")
            return
        try:
            if action_id == workflow_bank.WEB_MONITORING_FREE_ACTION_ID:
                results = web_search.search_public_news_rss(message_text)
            else:
                results = web_search.search_public_web_general(message_text)
            web_search_results_text = web_search.format_results_for_prompt(results)
        except WebSearchError as exc:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", f"web_search_{exc}")
            return
    # Un bouton de la banque (Phase 5) declenche SON PROPRE workflow n8n,
    # jamais celui du provider ChatGPT/Claude : les deux systemes ne doivent
    # jamais se confondre (voir le prompt qui a demande cette separation).
    elif action_id:
        try:
            workflow_record = workflow_bank.get_workflow_record_by_action(action_id)
        except RuntimeError:
            workflow_record = None
        if not workflow_record or not workflow_record.get("webhookPath") or not N8N_API_URL:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "action_workflow_not_configured")
            return
        webhook_url = f"{N8N_API_URL}/webhook/{workflow_record['webhookPath']}"
    else:
        webhook_url = N8N_WEBHOOK_CHATGPT_URL if model == "chatgpt" else N8N_WEBHOOK_CLAUDE_URL
        if not webhook_url:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "workflow_not_configured")
            return

    file_links = []
    for file_id in file_ids:
        expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
        try:
            token = sign_file_token(file_id, expires_at)
        except RuntimeError:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "file_signing_not_configured")
            return
        file_links.append(
            {
                "fileId": file_id,
                "url": f"{FRONTEND_URL}/api/workspace/files/{file_id}/signed?exp={expires_at}&token={token}",
            }
        )

    # Plateformes/API : logique GLOBALE et AUTOMATIQUE (demandee explicitement
    # pour tous les boutons, existants et futurs, sans jamais retoucher un
    # workflow n8n a chaque nouvelle API ajoutee). Contrairement a la version
    # precedente, on n'exige plus que l'utilisateur coche une connexion pour
    # qu'elle soit evaluee : TOUTES les connexions actives de la banque
    # partagee sont candidates a chaque execution, et seule la pertinence
    # (mots-cles vs demande + bouton) decide lesquelles sont reellement
    # appelees. connection_ids reste supporte comme filtre optionnel : si
    # l'utilisateur en coche explicitement, on restreint les candidates a ce
    # sous-ensemble plutot que la banque entiere (utile pour forcer/exclure).
    # Toujours calcule cote SERVEUR : jamais confie au frontend (§44).
    platform_data = []
    action_names = []
    if action_id:
        try:
            selected_action = workflow_bank.get_action(action_id)
        except RuntimeError:
            selected_action = None
        if selected_action:
            action_names.append(selected_action["name"])
    try:
        if connection_ids:
            candidate_connections = connections_bank.list_connections_by_ids(connection_ids)
        else:
            candidate_connections = connections_bank.list_connections()
    except RuntimeError:
        candidate_connections = []
    relevant_connections = platform_client.select_relevant_connections(
        candidate_connections, message_text, action_names
    )
    for connection in relevant_connections:
        try:
            resolved = connections_bank.get_connection_secret(connection["id"])
        except RuntimeError:
            resolved = None
        if not resolved:
            continue
        public_connection, secret = resolved
        data, error = platform_client.fetch_platform_data(public_connection, secret)
        # Une plateforme secondaire indisponible ne fait jamais echouer
        # toute la demande (prompt §45) : on le signale simplement dans
        # le contexte transmis, l'IA (ou l'humain qui lit metadata) voit
        # que cette source n'a pas pu etre utilisee.
        platform_data.append(
            {
                "connectionId": public_connection["id"],
                "name": public_connection["name"],
                "platformType": public_connection["platformType"],
                "used": error is None,
                "data": data,
                "error": error,
            }
        )

    if drive_document_text is not None:
        prompt_text = _build_drive_summary_prompt(message_text, drive_document_text)
    elif web_search_results_text is not None:
        prompt_text = _build_web_monitoring_prompt(message_text, web_search_results_text)
    else:
        prompt_text = _build_prompt_with_context(
            conversation_id, user_message_id, message_text, source_result_ids, reply_to_message_id
        )

    payload = {
        # Contrat minimal cote n8n : prompt/provider/conversationId/userId.
        "prompt": prompt_text,
        "provider": model,
        "conversationId": conversation_id,
        "userId": user_id,
        # Champs additionnels, deja utiles aux fonctionnalites existantes
        # (documents joints, enchainement de resultats, actions) : a
        # ignorer cote n8n si le workflow n'en a pas besoin.
        "requestId": request_id,
        "userName": user_name,
        "fileIds": file_ids,
        "files": file_links,
        "sourceResultIds": source_result_ids,
        "action": {"id": action_id, "type": action_id, "parameters": action_parameters} if action_id else None,
        # Uniquement les connexions selectionnees ET jugees pertinentes ;
        # une connexion selectionnee mais non pertinente n'apparait meme
        # pas ici (jamais appelee, prompt §21/43).
        "platformData": platform_data,
    }

    try:
        n8n_response = requests.post(
            webhook_url,
            json=payload,
            headers={"X-Agent-Stage-Secret": N8N_WEBHOOK_SECRET},
            timeout=N8N_TIMEOUT_SECONDS,
        )
        n8n_response.raise_for_status()
        n8n_data = n8n_response.json()
    except requests.exceptions.Timeout:
        _fail(run_id, conversation_id, user_message_id, request_id, "timeout", "n8n timeout")
        return
    except Exception as exc:
        _fail(run_id, conversation_id, user_message_id, request_id, "failed", str(exc)[:500])
        return

    blocks = n8n_data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        _fail(run_id, conversation_id, user_message_id, request_id, "failed", "empty_response")
        return

    result = workspace.create_result(
        conversation_id=conversation_id,
        message_id=user_message_id,
        user_id=user_id,
        model=model,
        workflow_type=action_id or model,
        request=payload,
        response={"blocks": blocks},
        source_result_ids=source_result_ids,
        metadata=n8n_data.get("metadata"),
    )

    workspace.add_message(
        conversation_id=conversation_id,
        user_id=None,
        author_name="ChatGPT" if model == "chatgpt" else "Claude",
        role="assistant",
        content=str(n8n_data.get("summary", ""))[:2000],
        blocks=blocks,
        model=model,
        action_id=action_id,
        result_id=result["id"],
        publish_extra={"requestId": request_id},
    )
    workspace.touch_conversation(conversation_id)
    workspace.complete_workflow_run(
        run_id, status="completed", result_id=result["id"], n8n_execution_id=n8n_data.get("executionId")
    )
