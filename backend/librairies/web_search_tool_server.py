"""
librairies/web_search_tool_server.py
=======================================

Serveur MCP minimal exposant UN outil REEL : "search_web" (recherche
publique, voir librairies/web_search.py -- meme moteur de recherche que les
boutons integres "Veille Web (sans API)" / "Veille Web (recherche
generale)", reutilise ici tel quel, jamais reimplemente en double).

But (mission "fonctionnement agentique de ChatGPT et Claude") : permettre a
l'IA de DECIDER ELLE-MEME, pendant sa reponse, si une recherche internet est
necessaire -- au lieu de forcer systematiquement une recherche cote serveur
AVANT l'appel au modele (comme le font les boutons integres), ou de ne
jamais pouvoir chercher du tout en conversation normale. Cet outil est
TOUJOURS attache a l'Agent des workflows n8n "chatgpt"/"claude" (voir
librairies/jobs.py, champ "webSearchToolUrl") -- sa simple presence
n'autorise PAS l'IA a l'utiliser pour tout et n'importe quoi, elle reste
libre de repondre directement quand aucune recherche n'est necessaire ;
c'est la description de l'outil ci-dessous, combinee a la consigne du
prompt cote n8n, qui guide cette decision.

IMPORTANT (mission §4) : ce mecanisme est ENTIEREMENT SEPARE des serveurs
MCP de la banque de connexions (voir connections_bank.py/jobs.py,
mcpServers/mcpExplicitlyRequested) -- jamais confondu avec eux. Le protocole
de transport (JSON-RPC MCP "HTTP+SSE" via Redis Pub/Sub) est identique a
librairies/mcp_stub_server.py par simple reutilisation technique (le noeud
"MCP Client" de n8n est deja un rouage de confiance dans ce projet), pas
parce que cet outil EST un serveur MCP configurable par l'utilisateur -- il
n'apparait jamais dans la banque de connexions, n'est jamais selectionnable,
et est cable en dur dans les workflows n8n eux-memes.
"""

from __future__ import annotations

import json
import os

import redis

from librairies import web_search

REDIS_URL = os.environ.get("REDIS_URL", "")

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None


def is_configured() -> bool:
    return _client is not None


def _channel(session_id: str) -> str:
    return f"web-search-tool:{session_id}"


def publish_response(session_id: str, response: dict | None) -> None:
    if response is None or _client is None:
        return
    _client.publish(_channel(session_id), json.dumps(response))


def subscribe(session_id: str):
    """Renvoie un objet pubsub deja abonne. L'appelant doit toujours faire
    pubsub.close() (dans un finally) a la deconnexion du flux SSE."""
    pubsub = _client.pubsub()
    pubsub.subscribe(_channel(session_id))
    return pubsub


_SEARCH_TOOL = {
    "name": "search_web",
    "description": (
        "Search the public web for current, recent, or verifiable information you do not already "
        "have -- current events, prices, dates, statistics, recent news, or anything that could have "
        "changed since your training. Do NOT call this for general knowledge you already know "
        "confidently, for creative writing, or for questions about data already provided in this "
        "conversation (attached documents, prior messages, platform data). Never invent search "
        "results -- if this tool returns no useful results or fails, say so honestly instead of "
        "guessing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, in the language most likely to return relevant results.",
            },
            "mode": {
                "type": "string",
                "enum": ["news", "general"],
                "description": (
                    "'news' for recent news articles (Google News RSS, best for \"latest/recent\" "
                    "questions) ; 'general' for general public web results (DuckDuckGo). Defaults to "
                    "'general' if omitted."
                ),
            },
        },
        "required": ["query"],
    },
}


def _run_search(arguments: dict) -> dict:
    query = str((arguments or {}).get("query") or "").strip()
    mode = str((arguments or {}).get("mode") or "general").strip().lower()
    if not query:
        return {"content": [{"type": "text", "text": "search_web error: empty query."}], "isError": True}
    try:
        if mode == "news":
            results = web_search.search_public_news_rss(query)
        else:
            results = web_search.search_public_web_general(query)
    except web_search.WebSearchError as exc:
        return {
            "content": [{"type": "text", "text": f"search_web failed ({exc}) -- do not invent results, tell the user this search did not succeed."}],
            "isError": True,
        }
    formatted = web_search.format_results_for_prompt(results)
    return {"content": [{"type": "text", "text": formatted}], "isError": False}


def handle_jsonrpc(request_body: dict) -> dict | None:
    """Meme structure que mcp_stub_server.handle_jsonrpc, avec un vrai
    resultat pour tools/call (voir _run_search ci-dessus) au lieu d'un
    placeholder."""
    method = request_body.get("method")
    request_id = request_body.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-stage-web-search-tool", "version": "1.0.0"},
        }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "tools/list":
        result = {"tools": [_SEARCH_TOOL]}
    elif method == "tools/call":
        if request_id is None:
            return None
        params = request_body.get("params") or {}
        if params.get("name") != "search_web":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"},
            }
        result = _run_search(params.get("arguments") or {})
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
