"""
librairies/excel_tool_server.py
==================================

Serveur MCP exposant UN outil REEL : "edit_excel" (mission Excel §5.3).
Meme architecture clonee que web_search_tool_server.py/rag_tool_server.py
(JSON-RPC MCP "HTTP+SSE" via Redis Pub/Sub). Contrairement au RAG, cet outil
produit un EFFET DE BORD (un nouveau fichier) pendant l'execution n8n, qui
doit ensuite etre rattache au message assistant cree APRES coup par le
worker RQ (librairies/jobs.py, process separe) -- pont assure ici par une
liste Redis a courte duree de vie, indexee par run_id (mission §5.3).
"""

from __future__ import annotations

import json
import os
import time
import uuid

import redis

from librairies import excel_tool

REDIS_URL = os.environ.get("REDIS_URL", "")
_PENDING_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None


def is_configured() -> bool:
    return _client is not None


def _channel(session_id: str) -> str:
    return f"excel-edit-tool:{session_id}"


def _session_key(session_id: str) -> str:
    return f"excel-edit-session:{session_id}"


def _pending_key(run_id: str) -> str:
    return f"excel-pending:{run_id}"


def _chain_key(run_id: str) -> str:
    return f"excel-chain:{run_id}"


def _resolve_chained_file_id(run_id: str, file_id: str) -> str:
    """Redirection automatique de chainage (bug observe en test E2E reel,
    reproduit et toujours present malgre l'indice textuel deja ajoute a la
    reponse de l'outil, cf. summary_text plus bas) : quand on demande a
    l'Agent d'enchainer plusieurs modifications Excel dans le MEME tour de
    conversation, il continue frequemment a renvoyer le fileId D'ORIGINE
    pour CHAQUE appel plutot que d'utiliser le nouveau fileId indique dans la
    reponse precedente -- verifie sur une execution n8n reelle
    (includeData=true) : les 3 appels d'un meme tour utilisaient tous et
    exactement le meme fileId d'origine. Un indice en texte libre dans la
    reponse de l'outil n'est pas fiable (le modele ne le relit pas toujours
    avant de formuler l'appel suivant). On maintient donc cote serveur, pour
    la duree de CE run uniquement, une table fileId -> dernier resultat
    connu pour ce fileId : si le fileId demande a deja ete modifie une fois
    pendant ce run, on redirige silencieusement vers son dernier resultat au
    lieu d'operer (a nouveau, a tort) sur l'original."""
    if _client is None:
        return file_id
    latest = _client.hget(_chain_key(run_id), file_id)
    return latest or file_id


def _record_chained_file(run_id: str, requested_file_id: str, new_file_id: str) -> None:
    if _client is None:
        return
    key = _chain_key(run_id)
    _client.hset(key, requested_file_id, new_file_id)
    _client.hset(key, new_file_id, new_file_id)  # identite : un id deja "a jour" se resout vers lui-meme
    _client.expire(key, _PENDING_TTL_SECONDS)


def _run_lock_key(run_id: str) -> str:
    return f"excel-run-lock:{run_id}"


def _acquire_run_lock(run_id: str, timeout_seconds: float = 25.0) -> str | None:
    """Verrou Redis (SET NX EX) qui serialise les appels edit_excel d'un MEME
    run_id -- necessaire car l'executeur d'outils de l'Agent (n8n/LangChain)
    peut envoyer PLUSIEURS appels edit_excel EN PARALLELE des qu'un seul tour
    du modele contient plusieurs tool_calls (constate sur une execution n8n
    reelle, includeData=true : les 3 appels d'un meme tour partageaient
    exactement le meme startTime). Sans ce verrou, la redirection de
    chainage (_resolve_chained_file_id/_record_chained_file, cf. plus haut)
    ne suffit pas : chaque appel concurrent lit l'etat AVANT que les autres
    n'aient eu le temps d'enregistrer leur propre resultat, donc tous
    continuent d'operer en parallele sur le fichier ORIGINAL. Le verrou force
    les appels concurrents a s'executer un par un, dans l'ordre ou ils
    parviennent reellement au serveur -- suffisant ici car les OPERATIONS
    elles-memes (indices de colonne, plages...) sont statiques (decidees par
    le modele en un seul coup, sans dependre du contenu intermediaire) ;
    seul le FICHIER sur lequel elles s'appliquent doit etre correctement
    enchaine. Renvoie None (jamais d'exception) si Redis est indisponible ou
    si le verrou n'a pas pu etre obtenu a temps -- l'appelant continue alors
    sans verrou plutot que d'echouer l'edition."""
    if _client is None:
        return None
    token = uuid.uuid4().hex
    key = _run_lock_key(run_id)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _client.set(key, token, nx=True, ex=30):
            return token
        time.sleep(0.15)
    return None


def _release_run_lock(run_id: str, token: str | None) -> None:
    if _client is None or not token:
        return
    key = _run_lock_key(run_id)
    # Ne supprime que si on detient encore le verrou (evite de supprimer le
    # verrou d'un autre appel si le notre a deja expire entre-temps).
    if _client.get(key) == token:
        _client.delete(key)


_LAST_RESULT_SENTINEL = "LAST_RESULT"


def _last_result_key(run_id: str) -> str:
    return f"excel-last-result:{run_id}"


def _get_last_result(run_id: str) -> str | None:
    if _client is None:
        return None
    return _client.get(_last_result_key(run_id))


def _set_last_result(run_id: str, file_id: str) -> None:
    if _client is None:
        return
    _client.setex(_last_result_key(run_id), _PENDING_TTL_SECONDS, file_id)


def register_session(session_id: str, user_id: str, run_id: str) -> None:
    if _client is None:
        return
    _client.setex(_session_key(session_id), _PENDING_TTL_SECONDS, json.dumps({"userId": user_id, "runId": run_id}))


def get_session_context(session_id: str) -> dict | None:
    if _client is None:
        return None
    raw = _client.get(_session_key(session_id))
    return json.loads(raw) if raw else None


def push_pending_file(run_id: str, file_id: str) -> None:
    if _client is None:
        return
    key = _pending_key(run_id)
    _client.rpush(key, file_id)
    _client.expire(key, _PENDING_TTL_SECONDS)


def pop_pending_files(run_id: str) -> list[str]:
    """Appele une fois par le worker RQ (jobs.py) apres la creation du
    message assistant -- vide et supprime la liste (jamais relu deux fois
    pour le meme run_id)."""
    if _client is None:
        return []
    key = _pending_key(run_id)
    values = _client.lrange(key, 0, -1)
    _client.delete(key)
    return values


def publish_response(session_id: str, response: dict | None) -> None:
    if response is None or _client is None:
        return
    _client.publish(_channel(session_id), json.dumps(response))


def subscribe(session_id: str):
    pubsub = _client.pubsub()
    pubsub.subscribe(_channel(session_id))
    return pubsub


_EDIT_EXCEL_TOOL = {
    "name": "edit_excel",
    "description": (
        "Modify an Excel (.xlsx) file that was attached to this conversation, using a constrained set of "
        "deterministic operations (never free-form code). Use this ONLY when the user explicitly asks to "
        "modify, transform, or compute something in a spreadsheet they attached -- never for merely reading "
        "or explaining its content (the document's content is already provided to you in the conversation "
        "context). This NEVER overwrites the original file -- it always produces a new file, which the user "
        "will see as a downloadable attachment automatically. Include the operation's summary and the "
        "returned preview table in your reply ; never claim a modification succeeded if this tool returned "
        "isError: true -- if it did, tell the user EXACTLY which step failed and why (quote the error), do not "
        "silently continue as if it had worked, and do not describe a fabricated result for that step.\n\n"
        "To chain several edits on the SAME file within one turn, you do not need to retype the previous call's "
        "result file id from memory (a mistyped id is silently rejected as 'access denied' and breaks the "
        "chain) -- pass the literal string \"LAST_RESULT\" as fileId instead, and it always resolves to the most "
        "recent file this tool produced in this conversation turn.\n\n"
        "'operation.type' selects which of operation's other fields are required -- ALWAYS include every "
        "field listed as required for the chosen type, never only 'type' alone:\n"
        "  set_cell: requires cell, value\n"
        "  set_range: requires range, values\n"
        "  add_column: requires header (optional: formula, startRow)\n"
        "  delete_column: requires column\n"
        "  add_row: requires values (optional: atRow)\n"
        "  delete_row: requires row\n"
        "  add_sheet: requires name\n"
        "  rename_sheet: requires oldName, newName\n"
        "  delete_sheet: requires name\n"
        "  write_formula: requires formula, and either cell or range\n"
        "  copy_range: requires sourceRange, destCell\n"
        "  sort_range: requires range, keyColumn (optional: ascending, hasHeader)\n"
        "  filter_rows: requires range, columnIndex, operator, value (optional: hasHeader)\n"
        "  clear_range: requires range\n"
        "'formula' values may use the literal placeholder {row} for the current row number, e.g. \"=B{row}-C{row}\"."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "fileId": {"type": "string", "description": "The id of the attached Excel file, given in the '(id: ...)' marker of the DOCUMENT JOINT header. To continue editing the result of a PREVIOUS edit_excel call in this SAME turn, pass the literal string \"LAST_RESULT\" instead of retyping that file's id."},
            "sheetName": {"type": "string", "description": "Target sheet name. Omit to use the active/first sheet."},
            "operation": {
                "type": "object",
                "description": "See the field-by-field requirements in the tool description above.",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "set_cell", "set_range", "add_column", "delete_column", "add_row", "delete_row",
                            "add_sheet", "rename_sheet", "delete_sheet", "write_formula", "copy_range",
                            "sort_range", "filter_rows", "clear_range",
                        ],
                        "description": "Which operation to perform. Determines which other fields below are required.",
                    },
                    "cell": {"type": "string", "description": "Cell reference (e.g. 'B2'). Required for set_cell ; alternative to 'range' for write_formula."},
                    "value": {"description": "Value to write (any JSON scalar). Required for set_cell. Also required for filter_rows (the value to compare each cell against)."},
                    "range": {"type": "string", "description": "Range reference (e.g. 'A1:C10'). Required for set_range, sort_range, filter_rows, clear_range ; alternative to 'cell' for write_formula."},
                    "values": {"type": "array", "description": "For set_range: a 2D array of row values, e.g. [[1,2],[3,4]]. For add_row: a flat array of values for the new row."},
                    "header": {"type": "string", "description": "New column's header text. Required for add_column."},
                    "formula": {"type": "string", "description": "Excel formula string, may use the placeholder {row}, e.g. '=B{row}-C{row}'. Required for write_formula ; optional for add_column (a computed column without a formula just gets the header)."},
                    "startRow": {"type": "integer", "description": "First data row to compute for add_column. Optional, defaults to 2 (row 1 is assumed to be the header row)."},
                    "column": {"type": "string", "description": "Column LETTER to delete (e.g. 'C'). Required for delete_column. Not used by filter_rows -- see 'columnIndex' for that."},
                    "columnIndex": {"type": "integer", "description": "1-based column NUMBER within 'range' to filter on (e.g. 2). Required for filter_rows. This is a number, never a letter."},
                    "atRow": {"type": "integer", "description": "Row index to insert the new row at, for add_row. Optional -- appends at the end if omitted."},
                    "row": {"type": "integer", "description": "Row index. Required for delete_row."},
                    "name": {"type": "string", "description": "Sheet name. Required for add_sheet (the new sheet's name) and delete_sheet (the sheet to delete)."},
                    "oldName": {"type": "string", "description": "Existing sheet name to rename. Required for rename_sheet."},
                    "newName": {"type": "string", "description": "New name for the sheet. Required for rename_sheet."},
                    "sourceRange": {"type": "string", "description": "Range to copy from. Required for copy_range."},
                    "destCell": {"type": "string", "description": "Top-left destination cell to copy into. Required for copy_range."},
                    "keyColumn": {"type": "integer", "description": "1-based column number within 'range' to sort by. Required for sort_range."},
                    "ascending": {"type": "boolean", "description": "Sort direction for sort_range. Optional, defaults to true."},
                    "hasHeader": {"type": "boolean", "description": "Whether the first row of 'range' is a header row to keep in place. Optional, defaults to true. Used by sort_range and filter_rows."},
                    "operator": {"type": "string", "enum": ["eq", "contains", "gt", "lt"], "description": "Comparison used by filter_rows between each row's 'column' cell and 'value'."},
                },
                "required": ["type"],
            },
        },
        "required": ["fileId", "operation"],
    },
}


def _run_edit(session_id: str, arguments: dict) -> dict:
    context = get_session_context(session_id)
    if not context:
        return {"content": [{"type": "text", "text": "edit_excel error: session not authorized."}], "isError": True}
    requested_file_id = str((arguments or {}).get("fileId") or "").strip()
    sheet_name = (arguments or {}).get("sheetName")
    operation = (arguments or {}).get("operation") or {}
    if not requested_file_id:
        return {"content": [{"type": "text", "text": "edit_excel error: fileId is required."}], "isError": True}

    run_id = context["runId"]
    # Verrou + redirection automatique de chainage -- voir
    # _acquire_run_lock/_resolve_chained_file_id : necessaire des que le
    # modele demande plusieurs modifications dependantes dans le meme tour,
    # car l'Agent envoie alors ces appels EN PARALLELE (confirme en test E2E
    # reel), donc seul le verrou garantit que chaque appel voit le resultat
    # REEL du precedent avant de choisir son propre fichier de depart.
    lock_token = _acquire_run_lock(run_id)
    try:
        is_sentinel = requested_file_id.strip().upper() == _LAST_RESULT_SENTINEL
        if is_sentinel:
            # Bug trouve en test E2E reel : meme avec le verrou + la
            # redirection ci-dessus, le modele peut simplement MAL RETRANSCRIRE
            # l'id du fichier precedent depuis son propre texte (un caractere
            # ou un groupe de caracteres manquant) -- aucune correspondance
            # exacte n'existe alors dans la table de chainage, donc aucune
            # redirection n'est possible et l'appel echoue avec "acces refuse
            # a ce fichier" (message trompeur : le vrai probleme est un id
            # invalide, pas un probleme de permission). Le sentinel
            # "LAST_RESULT" supprime le besoin de retranscrire quoi que ce
            # soit : il se resout toujours vers le dernier fichier reellement
            # produit par CE tour, quel que soit l'id que le modele a (ou
            # n'a pas) memorise.
            last_result = _get_last_result(run_id)
            if not last_result:
                return {
                    "content": [{
                        "type": "text",
                        "text": "edit_excel error: fileId=\"LAST_RESULT\" was used but no edit_excel call has "
                                "succeeded yet in this turn -- pass the real attached file's id for the first call.",
                    }],
                    "isError": True,
                }
            file_id = last_result
        else:
            file_id = _resolve_chained_file_id(run_id, requested_file_id)
        try:
            result = excel_tool.apply_edit(file_id, context["userId"], sheet_name, operation)
        except excel_tool.ExcelToolError as exc:
            return {"content": [{"type": "text", "text": f"edit_excel failed: {exc}"}], "isError": True}
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"edit_excel failed (unexpected error): {str(exc)[:200]}"}], "isError": True}

        push_pending_file(run_id, result["newFileId"])
        _record_chained_file(run_id, requested_file_id, result["newFileId"])
        _set_last_result(run_id, result["newFileId"])
    finally:
        _release_run_lock(run_id, lock_token)
    chain_note = (
        f" (poursuite automatique sur le dernier resultat de ce fichier dans cette conversation, "
        f"{file_id}, plutot que sur {requested_file_id})"
        if file_id != requested_file_id and not is_sentinel else ""
    )
    # Indice de chainage textuel : conserve en complement (jamais suffisant a
    # lui seul, cf. _resolve_chained_file_id -- verifie en test E2E reel que
    # le modele ne le relit pas toujours avant l'appel suivant), au cas ou
    # l'Agent enchaine correctement de lui-meme ou reference ce fichier dans
    # une prochaine reponse utilisateur (un nouveau run_id, donc hors
    # perimetre de la redirection automatique).
    summary_text = (
        f"{result['summary']}{chain_note}\nNouveau fichier : {result['newFileName']} (id: {result['newFileId']}) "
        f"(sera joint automatiquement a la reponse). Pour continuer a modifier CE resultat (une autre operation sur "
        f"le meme fichier), utilise fileId=\"{result['newFileId']}\" dans le prochain appel -- pas l'id du fichier "
        f"d'origine.\n\nApercu (feuille '{result['sheet']}') :\n{result['previewMarkdownTable']}"
    )
    return {"content": [{"type": "text", "text": summary_text}], "isError": False}


def handle_jsonrpc(request_body: dict, session_id: str) -> dict | None:
    method = request_body.get("method")
    request_id = request_body.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-stage-excel-edit-tool", "version": "1.0.0"},
        }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "tools/list":
        result = {"tools": [_EDIT_EXCEL_TOOL]}
    elif method == "tools/call":
        if request_id is None:
            return None
        params = request_body.get("params") or {}
        if params.get("name") != "edit_excel":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"},
            }
        result = _run_edit(session_id, params.get("arguments") or {})
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
