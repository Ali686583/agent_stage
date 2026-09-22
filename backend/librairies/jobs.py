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
import re
import time

import redis
import requests
from rq import Queue

from librairies import chart_render, connections_bank, excel_tool_server, google_drive, oauth_connector, platform_client, rag_tool_server, text_extraction, web_search, web_search_tool_server, workflow_bank, workspace
from librairies.google_drive import GoogleDriveConfigError, GoogleDriveError
from librairies.security import sign_file_token, sign_tool_token
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
# Entree 3 (serveur MCP) : un workflow n8n a une structure fixe (nombre de
# blocs "MCP Client" fige a la conception, voir librairies/connections_bank.py),
# jamais un nombre illimite de connexions simultanees.
MAX_MCP_SERVERS_PER_REQUEST = 5
# Correction architecture MCP (2026-09-21) : le mecanisme Agent+MCP ne doit
# JAMAIS s'activer simplement parce qu'un serveur MCP est connecte -- voir
# _mcp_explicitly_requested_in_entry ci-dessous et son usage dans
# execute_workflow_run. Toutes les formulations attendues ("le MCP X",
# "un serveur MCP", "l'outil MCP") contiennent le mot "MCP" lui-meme.
_MCP_EXPLICIT_TRIGGER_RE = re.compile(r"\bmcp\b", re.IGNORECASE)
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


def _mcp_explicitly_requested_in_entry(message_text: str) -> bool:
    """Cas A (correction architecture MCP, mission §3) : le MCP ne doit
    jamais se declencher a cause de la simple disponibilite/pertinence
    d'une connexion -- seulement si l'utilisateur ecrit explicitement le
    mot "MCP" dans sa demande ("le MCP X", "un serveur MCP", "l'outil
    MCP"...). Volontairement un simple mot-cle (coherent avec le reste de
    ce module, qui n'a pas de comprehension semantique du langage
    naturel) plutot qu'une liste de formulations fixes."""
    return bool(_MCP_EXPLICIT_TRIGGER_RE.search(message_text or ""))


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


def _extract_attachment_text(file_id: str, download_url: str) -> tuple[str | None, str | None]:
    """Contenu REEL d'une piece jointe (mission "correction architecture
    MCP" §2 : "le contenu pertinent du document doit etre fourni a l'IA...
    independamment du MCP"). Ne leve jamais et n'invente jamais un contenu :
    un format non pris en charge ou un telechargement en echec renvoie une
    erreur explicite (texte, code), jamais un texte fabrique. Le worker RQ
    n'a pas acces au volume de stockage (service Railway separe du service
    web, voir railway volume list) : le fichier est donc recupere via son
    URL signee, exactement comme n8n le fait deja pour les boutons de la
    banque (voir file_links plus bas).

    Mince enveloppe autour de librairies/text_extraction.py (mission RAG
    §3/§4b) : le telechargement reste ici (specifique a ce worker), mais la
    logique "octets -> texte" est partagee telle quelle avec l'ingestion RAG,
    jamais dupliquee."""
    try:
        file_record = workspace.get_file(file_id)
    except Exception:
        file_record = None
    if not file_record:
        return None, "fichier introuvable"
    name = file_record.get("name") or file_id
    mime_type = file_record.get("mimeType") or ""

    try:
        response = requests.get(download_url, timeout=20)
        response.raise_for_status()
        raw = response.content
    except requests.exceptions.RequestException as exc:
        return None, f"telechargement impossible ({str(exc)[:150]})"

    return text_extraction.extract_text(raw, name, mime_type)


def _build_attached_documents_context(file_ids: list[str], file_links: list[dict]) -> str:
    """Un bloc par piece jointe, dans l'ORDRE fourni -- jamais melange au
    reste du prompt de facon a en perdre la source (mission §2 : "les
    documents joints font partie du contexte du message"). Fonctionne
    entierement independamment du MCP (aucun serveur MCP implique ici)."""
    links_by_id = {link["fileId"]: link["url"] for link in file_links}
    parts = []
    for file_id in file_ids:
        download_url = links_by_id.get(file_id)
        if not download_url:
            continue
        text, error = _extract_attachment_text(file_id, download_url)
        try:
            file_record = workspace.get_file(file_id)
        except Exception:
            file_record = None
        display_name = (file_record or {}).get("name") or file_id
        if error:
            parts.append(f"--- DOCUMENT JOINT : {display_name} (id: {file_id}) (INDISPONIBLE : {error}) ---")
        else:
            parts.append(f"--- DOCUMENT JOINT : {display_name} (id: {file_id}) ---\n{text}\n--- FIN DU DOCUMENT ---")
    if not parts:
        return ""
    return (
        "Document(s) reellement joint(s) par l'utilisateur a ce message (contenu "
        "extrait automatiquement). Utilise ce contenu REEL pour repondre ; si un "
        "document est marque INDISPONIBLE, dis-le clairement plutot que "
        "d'inventer son contenu.\n\n" + "\n\n".join(parts)
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

    # Outil "recherche web" agentique (mission "fonctionnement agentique de
    # ChatGPT et Claude") : disponible pour les webhooks PRINCIPAUX
    # ChatGPT/Claude uniquement (mode normal, Resume Drive, Veille Web) --
    # jamais pour un bouton de la banque avec son propre workflow n8n dedie
    # (ceux-ci gardent leur propre logique de donnees, ex. SPS/Resumer PDF,
    # jamais transformes en agent sans que ce soit demande). Toujours REEL :
    # jamais un placeholder, contrairement aux emplacements MCP non utilises
    # (voir mcp_servers plus bas).
    # Repli sur le meme point d'ancrage neutre que les emplacements MCP non
    # utilises (voir mcp_stub_server.py) si l'outil reel ne peut pas etre
    # signe (config manquante) : le graphe n8n reste TOUJOURS valide (un
    # noeud "MCP Client" pointant vers une URL vide/injoignable fait echouer
    # tout l'Agent, verifie en conditions reelles) plutot que d'omettre le
    # champ.
    is_main_provider_webhook = webhook_url in (N8N_WEBHOOK_CHATGPT_URL, N8N_WEBHOOK_CLAUDE_URL)
    web_search_tool_url = f"{FRONTEND_URL}/api/workspace/mcp/stub"
    if is_main_provider_webhook and web_search_tool_server.is_configured():
        expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
        try:
            tool_token = sign_tool_token("web-search-tool", expires_at)
        except RuntimeError:
            tool_token = None
        if tool_token:
            web_search_tool_url = f"{FRONTEND_URL}/api/workspace/mcp/web-search?exp={expires_at}&token={tool_token}"

    # Outils agentiques RAG et Excel (mission RAG §4d/§4h, mission Excel §5.4) :
    # MEME principe et MEME restriction que l'outil de recherche web
    # ci-dessus (champ dedie toujours attache aux webhooks PRINCIPAUX
    # uniquement, jamais insere dans mcpServers -- gate different, reserve
    # aux connexions de la banque). Le token encode en plus user_id (RAG,
    # pour l'autorisation) et user_id+run_id (Excel, pour retrouver le
    # fichier genere une fois l'execution n8n terminee, voir plus bas).
    rag_search_tool_url = f"{FRONTEND_URL}/api/workspace/mcp/stub"
    if is_main_provider_webhook and rag_tool_server.is_configured():
        expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
        try:
            tool_token = sign_tool_token(f"rag-search:{user_id}", expires_at)
        except RuntimeError:
            tool_token = None
        if tool_token:
            rag_search_tool_url = (
                f"{FRONTEND_URL}/api/workspace/mcp/rag?exp={expires_at}&token={tool_token}&uid={user_id}"
            )

    excel_edit_tool_url = f"{FRONTEND_URL}/api/workspace/mcp/stub"
    if is_main_provider_webhook and excel_tool_server.is_configured():
        expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
        try:
            tool_token = sign_tool_token(f"excel-edit:{user_id}:{run_id}", expires_at)
        except RuntimeError:
            tool_token = None
        if tool_token:
            excel_edit_tool_url = (
                f"{FRONTEND_URL}/api/workspace/mcp/excel-edit?exp={expires_at}&token={tool_token}&uid={user_id}&run={run_id}"
            )

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
    # Entree 3 (serveur MCP, voir connections_bank.py) : contrairement aux
    # entrees 1/2, ce backend n'appelle jamais le serveur lui-meme -- n8n le
    # fait directement (bloc "AI Agent" + "MCP Client", qui choisit lui-meme
    # QUAND et QUEL outil appeler pendant la generation de la reponse). On se
    # contente ici de rassembler la configuration (secret dechiffre inclus)
    # des connexions pertinentes et de la transmettre telle quelle. Un
    # workflow n8n a une structure fixe (nombre de blocs "MCP Client" fige a
    # la conception) : on plafonne donc a MAX_MCP_SERVERS_PER_REQUEST, jamais
    # un nombre illimite.
    #
    # CORRECTION ARCHITECTURE MCP (2026-09-21) : la pertinence par mot-cle
    # ci-dessus reste utilisee telle quelle pour platform_data (entrees 1/2,
    # INCHANGE), mais ne suffit plus a elle seule a attacher un serveur MCP
    # (entree 3) -- voir mission "preserver le comportement normal de
    # ChatGPT/Claude". Deux cas UNIQUEMENT (mission §3) :
    #   Cas A -- mode normal / actions integrees (Resume Drive, Veille Web) :
    #     ces requetes utilisent les MEMES webhooks principaux
    #     N8N_WEBHOOK_CHATGPT_URL/CLAUDE_URL, dont le workflow n8n ne route
    #     desormais vers l'Agent+MCP QUE si l'utilisateur a explicitement
    #     ecrit "MCP" dans sa demande (voir _mcp_explicitly_requested_in_entry
    #     et le flag mcpExplicitlyRequested plus bas).
    #   Cas B -- bouton de la banque avec SON PROPRE workflow n8n (webhook
    #     dedie, ligne ~320) : la decision "utiliser MCP ou non" appartient
    #     entierement a CE workflow n8n (ses propres noeuds) -- ce backend
    #     continue donc de fournir les connexions MCP pertinentes comme
    #     avant, un workflow qui ne les consulte jamais (prompt classique,
    #     ex. SPS/Veille brevets/Resumer PDF) n'en fait simplement rien.
    is_bank_button_workflow = bool(action_id) and action_id not in (
        workflow_bank.GOOGLE_DRIVE_ACTION_ID,
        workflow_bank.WEB_MONITORING_FREE_ACTION_ID,
        workflow_bank.WEB_MONITORING_GENERAL_ACTION_ID,
    )
    mcp_explicitly_requested = is_bank_button_workflow or _mcp_explicitly_requested_in_entry(message_text)
    mcp_servers = []
    for connection in relevant_connections:
        try:
            resolved = connections_bank.get_connection_secret(connection["id"])
        except RuntimeError:
            resolved = None
        if not resolved:
            continue
        public_connection, secret = resolved
        # Entrees 1 (cle API) et 2 (OAuth) sont independantes et facultatives
        # (voir connections_bank.py) : la cle API est essayee en premier si
        # presente, sinon on retombe sur un jeton OAuth deja connecte -- une
        # connexion sans aucune des deux remonte "no_credential_configured"
        # (voir platform_client.py), jamais une erreur opaque.
        credential = secret or None
        if not credential:
            try:
                credential = oauth_connector.get_valid_access_token(connection["id"])
            except RuntimeError:
                credential = None
        data, error = platform_client.fetch_platform_data(public_connection, credential)
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

        if mcp_explicitly_requested and len(mcp_servers) < MAX_MCP_SERVERS_PER_REQUEST:
            try:
                mcp_config = connections_bank.get_mcp_config_with_secret(connection["id"])
            except RuntimeError:
                mcp_config = None
            if mcp_config:
                mcp_servers.append(mcp_config)

    # Le workflow n8n a un nombre FIXE de blocs "MCP Client" (voir plus haut) :
    # verifie en conditions reelles qu'un bloc pointant vers une URL vide ou
    # injoignable fait echouer TOUT l'Agent IA, meme pour une demande qui n'a
    # besoin d'AUCUN serveur MCP (les emplacements non utilises restent
    # branches au meme Agent). On comble donc systematiquement les
    # emplacements restants avec un serveur MCP "vide" toujours disponible
    # (voir librairies/mcp_stub_server.py), plutot que de laisser un
    # emplacement vide qui casserait CHAQUE reponse.
    while len(mcp_servers) < MAX_MCP_SERVERS_PER_REQUEST:
        mcp_servers.append(
            {
                "name": "",
                "serverUrl": f"{FRONTEND_URL}/api/workspace/mcp/stub",
                "transport": "sse",
                "authLocation": "none",
                "authFieldName": "",
                "secret": "",
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
        # Pieces jointes en mode normal (mission "correction architecture
        # MCP" §2) : le contenu REEL est fourni a l'IA ici, cote backend --
        # jamais via le mecanisme MCP (une piece jointe ne doit jamais
        # provoquer d'appel MCP), et jamais pour un bouton de la banque (qui
        # a deja sa propre logique de fichier dans son propre workflow n8n,
        # ex. SPS/Resumer PDF -- ajouter cette extraction cote Python ferait
        # double emploi et pourrait diverger de son traitement specifique).
        if not action_id and file_ids:
            attached_documents_text = _build_attached_documents_context(file_ids, file_links)
            if attached_documents_text:
                prompt_text = f"{attached_documents_text}\n\n{prompt_text}"
    # Politique de graphiques (voir librairies/chart_render.py) : injectee
    # dans CHAQUE prompt, quelle que soit son origine (entry, bouton
    # integre, ou resume Drive/veille web) -- un bouton/workflow qui a
    # besoin d'un graphique n'a jamais a etre reecrit pour ca, l'IA voit
    # toujours cette politique. Les workflows n8n PERSONNALISES (boutons
    # existants avec leur propre prompt fige, qui n'interpolent pas tous
    # {{ $json.body.prompt }}) recoivent en plus cette meme politique
    # directement dans leur propre prompt (voir la mise a jour de ces
    # workflows n8n, hors de ce depot).
    prompt_text = f"{prompt_text}\n\n{chart_render.CHART_POLICY}"

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
        # Entree 3 (serveur MCP) : secret dechiffre INCLUS ici -- necessaire
        # (voir plus haut, n8n appelle lui-meme le serveur), mais ce dict
        # complet ne doit JAMAIS etre persiste tel quel (voir stored_payload
        # ci-dessous, qui redige ce champ avant l'ecriture en base).
        "mcpServers": mcp_servers,
        # Correction architecture MCP : indique EXPLICITEMENT a n8n (workflows
        # "chatgpt"/"claude" principaux) s'il faut router vers le mecanisme
        # Agent+MCP ou vers un appel direct classique -- jamais laisse a la
        # seule presence/pertinence d'une connexion MCP dans mcpServers.
        "mcpExplicitlyRequested": mcp_explicitly_requested,
        # Outil de recherche web agentique (voir plus haut) : None pour un
        # bouton de la banque (webhook dedie), une URL SSE signee sinon --
        # c'est le workflow n8n qui decide de l'attacher ou non a l'Agent,
        # jamais ce backend qui force une recherche.
        "webSearchToolUrl": web_search_tool_url,
        # Outil "recherche documentaire interne" agentique (mission RAG §4i) :
        # meme principe que webSearchToolUrl -- c'est le workflow n8n qui
        # decide de l'attacher a l'Agent, jamais ce backend qui force une
        # recherche RAG.
        "ragSearchToolUrl": rag_search_tool_url,
        # Outil "modification Excel" agentique (mission Excel §5.6).
        "excelEditToolUrl": excel_edit_tool_url,
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
    # Extrait les graphiques Matplotlib eventuellement inclus par l'IA (voir
    # chart_render.CHART_POLICY) : chaque bloc "markdown" est eclate en une
    # sequence ordonnee texte/image la ou un ```chart_spec``` valide est
    # trouve -- jamais deplace, jamais invente (une specification invalide
    # laisse le texte original tel quel, voir split_markdown_into_blocks).
    # Les autres types de blocs (table, chart Chart.js existant, etc.) ne
    # sont jamais modifies.
    expanded_blocks = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "markdown" and "```chart_spec" in (block.get("content") or ""):
            expanded_blocks.extend(chart_render.split_markdown_into_blocks(block["content"]))
        else:
            expanded_blocks.append(block)
    blocks = expanded_blocks

    # RÈGLE ABSOLUE (meme principe que connections_bank.py) : un secret MCP
    # dechiffre ne doit jamais etre persiste en base, meme si `payload` (qui
    # le contient, necessairement, pour que n8n puisse authentifier son appel
    # au serveur) a deja ete envoye tel quel a n8n ci-dessus. `results.request`
    # est lisible par tout participant de la conversation (voir
    # workspace_routes.get_result_route) : on y stocke une version redigee.
    stored_payload = dict(payload)
    stored_payload["mcpServers"] = [
        {**{k: v for k, v in server.items() if k != "secret"}, "hasSecret": bool(server.get("secret"))}
        for server in mcp_servers
    ]
    # Meme principe pour le lien signe de l'outil de recherche web (courte
    # duree de vie, mais un lien signe valide reste un lien signe valide) :
    # jamais persiste tel quel -- seul le repli neutre (voir plus haut, pas
    # de token) est laisse visible, sinon juste un booleen.
    stored_payload["webSearchToolUrl"] = (
        web_search_tool_url if web_search_tool_url.endswith("/mcp/stub") else True
    )
    # Meme redaction pour les liens signes RAG/Excel (mission §9, secret
    # audit) : jamais persistes tels quels, meme de courte duree de vie.
    stored_payload["ragSearchToolUrl"] = (
        rag_search_tool_url if rag_search_tool_url.endswith("/mcp/stub") else True
    )
    stored_payload["excelEditToolUrl"] = (
        excel_edit_tool_url if excel_edit_tool_url.endswith("/mcp/stub") else True
    )

    result = workspace.create_result(
        conversation_id=conversation_id,
        message_id=user_message_id,
        user_id=user_id,
        model=model,
        workflow_type=action_id or model,
        request=stored_payload,
        response={"blocks": blocks},
        source_result_ids=source_result_ids,
        metadata=n8n_data.get("metadata"),
    )

    # "content" (texte brut de repli, ex. apercu "en reponse a") ne doit
    # jamais afficher le JSON brut d'un chart_spec -- remplace par un
    # marqueur lisible, le rendu reel restant dans "blocks" ci-dessus.
    plain_summary = chart_render.CHART_SPEC_FENCE_RE.sub("[Graphique]", str(n8n_data.get("summary", "")))

    assistant_message = workspace.add_message(
        conversation_id=conversation_id,
        user_id=None,
        author_name="ChatGPT" if model == "chatgpt" else "Claude",
        role="assistant",
        content=plain_summary[:2000],
        blocks=blocks,
        model=model,
        action_id=action_id,
        result_id=result["id"],
        publish_extra={"requestId": request_id},
    )
    workspace.touch_conversation(conversation_id)
    # Fichier(s) genere(s) par l'outil Excel pendant cette execution (mission
    # Excel §5.5) : la piece jointe assistant n'existe que si l'Agent a
    # REELLEMENT appele edit_excel (liste vide sinon -- aucun changement de
    # comportement pour une reponse normale). renderMessage() cote frontend
    # affiche deja les pieces jointes de n'importe quel role, donc rien
    # d'autre a faire pour que le bouton de telechargement apparaisse.
    try:
        pending_file_ids = excel_tool_server.pop_pending_files(run_id)
    except Exception:
        pending_file_ids = []
    if pending_file_ids:
        workspace.link_files_to_message(assistant_message["id"], pending_file_ids)
    workspace.complete_workflow_run(
        run_id, status="completed", result_id=result["id"], n8n_execution_id=n8n_data.get("executionId")
    )
