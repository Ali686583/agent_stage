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

from librairies import connections_bank, platform_client, workflow_bank, workspace
from librairies.security import sign_file_token

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


def _build_prompt_with_reply_context(message_text: str, source_result_ids: list[str]) -> str:
    """Bouton "Repondre" (mission §5) : sans ceci, sourceResultIds n'etait
    transmis a n8n que comme un id opaque que les workflows actuels
    n'exploitent pas -- l'IA n'avait donc aucune idee de QUELLE reponse
    l'utilisateur visait. Plutot que de modifier chaque workflow n8n (le
    "chatgpt"/"claude" par defaut, plus tous les boutons de la banque) pour
    qu'ils sachent resoudre un sourceResultId, on enrichit ici le SEUL champ
    que tous lisent deja (`prompt`) : aucune modification n8n necessaire,
    fonctionne immediatement avec tout workflow existant ou futur."""
    if not source_result_ids:
        return message_text
    referenced_texts = []
    for result_id in source_result_ids:
        try:
            referenced_result = workspace.get_result(result_id)
        except Exception:
            referenced_result = None
        if not referenced_result:
            continue
        text = _extract_result_text(referenced_result)
        if text:
            referenced_texts.append(text)
    if not referenced_texts:
        return message_text
    quoted = "\n\n---\n\n".join(referenced_texts)
    return (
        "L'utilisateur repond specifiquement a la reponse precedente suivante "
        "(contexte a prendre en compte, ne pas la reproduire telle quelle) :\n"
        f"{quoted}\n\n"
        "Nouvelle demande de l'utilisateur, en reponse a ce contexte precis :\n"
        f"{message_text}"
    )


def _fail(run_id: str, conversation_id: str, message_id: str, request_id: str, status: str, error: str) -> None:
    workspace.complete_workflow_run(run_id, status=status, error=error)
    try:
        workspace.publish_workflow_failed(conversation_id, request_id, message_id, error)
    except Exception:
        pass  # best-effort : le run reste correctement marque en base malgre tout


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
) -> None:
    # Un bouton de la banque (Phase 5) declenche SON PROPRE workflow n8n,
    # jamais celui du provider ChatGPT/Claude : les deux systemes ne doivent
    # jamais se confondre (voir le prompt qui a demande cette separation).
    if action_id:
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

    prompt_text = _build_prompt_with_reply_context(message_text, source_result_ids)

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
