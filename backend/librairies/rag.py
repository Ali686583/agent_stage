"""
librairies/rag.py
===================

Systeme RAG documentaire (mission RAG, Phase 4). Deux responsabilites :

1. Ingestion (`ingest_document`) : texte -> normalisation -> decoupage en
   chunks -> embeddings OpenAI -> stockage (`document_chunks`). Appele par
   librairies/rag_jobs.py (worker RQ, file d'attente separee
   "agent_stage_rag" -- jamais melangee a "agent_stage_workflows", pour ne
   jamais retarder une reponse de chat, voir jobs.py).

2. Recuperation (`search_documents`) : requete -> chunks pertinents,
   filtres par autorisation AVANT tout retour a l'appelant (§4d, "POINT
   CRITIQUE" -- jamais une liste non filtree qu'on filtrerait apres coup).

Le worker RQ n'a pas acces au volume de stockage (service Railway separe du
service web, meme contrainte documentee dans jobs.py) : un document est donc
toujours relu via son URL de fichier SIGNEE, jamais lu directement sur
disque, meme pour un document "standalone" uploade directement dans la
Documentation.

Autorisation (§4d, decision produit explicite) : NE REUTILISE PAS le modele
large de workspace.user_can_access_file ("une fois attache a un message
n'importe ou, tout utilisateur authentifie y accede"). Un document
"attachment" n'est visible en RAG que pour son proprietaire, ou pour un
participant REEL de la conversation a laquelle il est rattache (meme
semantique que workspace.is_participant, mais lue directement en SQL ici
pour un filtrage a la source, sans effet de bord d'auto-inscription). Un
document "standalone" n'est visible que pour son proprietaire, ou si
`is_shared = true`. Le meme helper d'ACCES (`_ACCESS_SQL`) est reutilise pour
la liste "Documentation" (workspace_routes.py) : les deux ne doivent jamais
diverger sur QUI peut voir/recuperer un document. Seule la recuperation RAG
ajoute en plus `status = 'READY'` (`_VISIBILITY_SQL`) -- la liste
Documentation montre sciemment aussi les documents pas encore indexes ou en
echec (mission §17/18).
"""

from __future__ import annotations

import logging
import os
import time

import requests

from librairies.database import _db
from librairies.security import sign_file_token
from librairies.workspace import new_id

# Bug trouve en test E2E reel : le web (gunicorn, server.py::database.init_db)
# et le worker RQ (qui n'appelle JAMAIS init_db -- rien dans rag_jobs.py ne le
# fait) peuvent avoir des vues DIFFERENTES de pgvector_available() -- le
# worker la voit toujours comme indisponible (son process ne sonde jamais),
# alors que le web peut la voir comme disponible. Or document_chunks est
# cree UNE SEULE FOIS (CREATE TABLE IF NOT EXISTS) avec un type de colonne
# fige a ce moment-la : si le process qui ingere (worker, toujours "False")
# et celui qui recupere (web, potentiellement "True") desaccordent, on
# obtient une erreur Postgres "operateur incompatible" (<=> n'existe que
# pour le type vector). Plutot que de faire confiance a un indicateur par
# PROCESS qui peut diverger du SCHEMA reellement persiste, on interroge la
# verite du schema une fois et on la met en cache -- utilise par
# ingest_document ET search_documents, jamais l'un sans l'autre.
_embedding_is_vector_cache: bool | None = None


def _embedding_column_is_vector() -> bool:
    global _embedding_is_vector_cache
    if _embedding_is_vector_cache is not None:
        return _embedding_is_vector_cache
    with _db() as conn:
        row = conn.execute(
            """SELECT data_type, udt_name FROM information_schema.columns
               WHERE table_name = 'document_chunks' AND column_name = 'embedding'"""
        ).fetchone()
    _embedding_is_vector_cache = bool(row) and row["udt_name"] == "vector"
    return _embedding_is_vector_cache


def _ensure_vector_adapter(conn) -> None:
    """database._db() n'enregistre l'adaptateur pgvector (register_vector)
    que si LE PROCESS COURANT pense pgvector disponible (_PGVECTOR_AVAILABLE,
    jamais mis a jour dans le worker RQ -- voir commentaire plus haut). Ici
    on se base sur la verite du schema (_embedding_column_is_vector), donc
    on doit re-garantir l'adaptateur nous-memes sur CETTE connexion,
    independamment de ce que _db() a deja fait ou non. register_vector est
    sans effet indesirable a rappeler plusieurs fois sur la meme connexion."""
    from pgvector.psycopg import register_vector

    register_vector(conn)

_logger = logging.getLogger(__name__)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
FILE_LINK_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536

_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 150

# Meme condition SQL utilisee par search_documents() ET par la route de
# listing "Documentation" (workspace_routes.py::list_documents_route) --
# jamais deux definitions qui pourraient diverger (§4c/§7, "listing et RAG ne
# doivent jamais diverger"). `%(user_id)s` est le seul parametre attendu.
# ATTENTION : ce clause ne couvre QUE l'acces (proprietaire / partage /
# participant) -- jamais le statut d'indexation. Corrige apres un bug trouve
# en test E2E reel : la premiere version incluait `d.status = 'READY'` ici,
# ce qui faisait disparaitre un document de la liste Documentation tant
# qu'il n'etait pas encore indexe (UPLOADED/PROCESSING) ou si son indexation
# avait echoue (FAILED) -- alors meme que l'UI Documentation a ses propres
# badges de statut pour ces cas (mission §17/18, l'utilisateur doit
# justement pouvoir VOIR qu'un document est en cours ou en echec). Seule la
# RECUPERATION RAG (search_documents, qui a besoin de chunks reellement
# indexes) doit encore filtrer sur status='READY' -- voir son usage
# ci-dessous, qui ajoute cette condition separement.
_ACCESS_SQL = """
    (
        (d.source_type = 'attachment' AND (
            d.owner_user_id = %(user_id)s
            OR (d.conversation_id IS NOT NULL AND (
                EXISTS (
                    SELECT 1 FROM conversations c
                    WHERE c.id = d.conversation_id AND c.is_shared = true
                )
                OR EXISTS (
                    SELECT 1 FROM conversation_participants cp
                    WHERE cp.conversation_id = d.conversation_id AND cp.user_id = %(user_id)s
                )
            ))
        ))
        OR (d.source_type = 'standalone' AND (d.owner_user_id = %(user_id)s OR d.is_shared = true))
    )
"""
# Alias conserve pour compatibilite de lecture : la recuperation RAG doit
# TOUJOURS filtrer sur les deux conditions (acces ET indexe).
_VISIBILITY_SQL = f"d.status = 'READY' AND {_ACCESS_SQL}"


class RagError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Documents (metadonnees)
# ---------------------------------------------------------------------------

def create_document(
    *,
    name: str,
    mime_type: str | None,
    size_bytes: int | None,
    owner_user_id: str,
    source_type: str,
    file_id: str,
    conversation_id: str | None = None,
    message_id: str | None = None,
    project_id: str | None = None,
    is_shared: bool = False,
) -> dict:
    document_id = new_id("doc")
    with _db() as conn:
        conn.execute(
            """INSERT INTO documents
               (id, name, mime_type, size_bytes, owner_user_id, source_type,
                file_id, conversation_id, message_id, project_id, is_shared)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                document_id, name, mime_type, size_bytes, owner_user_id, source_type,
                file_id, conversation_id, message_id, project_id, is_shared,
            ),
        )
    return get_document(document_id)


def get_document(document_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
    return _public_document(row) if row else None


def backfill_attachment_documents(file_ids: list[str], conversation_id: str, message_id: str) -> None:
    """Appele par workspace.link_files_to_message (mission RAG §4c) : au
    moment de l'upload (POST /files), la piece jointe n'est pas encore
    rattachee a un message/une conversation -- seul son proprietaire y a
    donc acces. Des qu'elle est effectivement jointe a un message, on
    complete ces colonnes pour que l'autorisation RAG (participants de LA
    conversation) s'applique enfin."""
    if not file_ids:
        return
    with _db() as conn:
        conn.execute(
            """UPDATE documents
               SET conversation_id = %s, message_id = %s, updated_at = now()
               WHERE file_id = ANY(%s) AND source_type = 'attachment'""",
            (conversation_id, message_id, file_ids),
        )


def _public_document(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "mimeType": row.get("mime_type"),
        "sizeBytes": row.get("size_bytes"),
        "ownerUserId": row["owner_user_id"],
        "sourceType": row["source_type"],
        "fileId": row.get("file_id"),
        "conversationId": row.get("conversation_id"),
        "messageId": row.get("message_id"),
        "projectId": row.get("project_id"),
        "isShared": bool(row.get("is_shared")),
        "status": row["status"],
        "statusError": row.get("status_error"),
        "contentVersion": row.get("content_version"),
        "indexedVersion": row.get("indexed_version"),
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def list_visible_documents(user_id: str, *, search: str | None = None, limit: int = 30, before: str | None = None) -> list[dict]:
    """Liste "Documentation" (mission §7) : reutilise la MEME regle d'ACCES
    que search_documents (_ACCESS_SQL) -- jamais de derive entre "qui peut
    voir ce document dans la bibliotheque" et "qui peut le recuperer via le
    RAG". Contrairement a search_documents, n'exige PAS status='READY' : un
    document UPLOADED/PROCESSING/FAILED doit rester visible ici (avec son
    badge de statut, voir workspace.js renderDocumentRow) pour que
    l'utilisateur voie qu'il est en cours d'indexation ou en echec -- seule
    la recuperation de chunks reels a besoin d'un index pret. Metadonnees
    uniquement (jamais extracted_text, §7 "ne jamais charger tout le contenu
    pour un simple listing"). Curseur de pagination : `before` est le
    `createdAt` ISO du dernier element de la page precedente."""
    clauses = [_ACCESS_SQL]
    params: dict = {"user_id": user_id}
    if search:
        clauses.append("d.name ILIKE %(search)s")
        params["search"] = f"%{search}%"
    if before:
        clauses.append("d.created_at < %(before)s")
        params["before"] = before
    where_sql = " AND ".join(f"({c})" for c in clauses)
    with _db() as conn:
        rows = conn.execute(
            f"""SELECT d.id, d.name, d.mime_type, d.size_bytes, d.owner_user_id, d.source_type,
                       d.conversation_id, d.project_id, d.is_shared, d.status, d.status_error,
                       d.created_at, d.updated_at
                FROM documents d
                WHERE {where_sql}
                ORDER BY d.created_at DESC
                LIMIT %(limit)s""",
            {**params, "limit": min(max(limit, 1), 100)},
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "mimeType": r.get("mime_type"),
            "sizeBytes": r.get("size_bytes"),
            "sourceType": r["source_type"],
            "conversationId": r.get("conversation_id"),
            "projectId": r.get("project_id"),
            "isShared": bool(r.get("is_shared")),
            "status": r["status"],
            "statusError": r.get("status_error"),
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
            "isOwner": r["owner_user_id"] == user_id,
        }
        for r in rows
    ]


def rename_document(document_id: str, name: str) -> dict | None:
    with _db() as conn:
        conn.execute("UPDATE documents SET name = %s, updated_at = now() WHERE id = %s", (name, document_id))
    return get_document(document_id)


def user_can_manage_document(document_id: str, user_id: str) -> bool:
    """Rename/supprime/reindexe : uniquement le proprietaire (§7, "avec
    autorisation explicite"). Volontairement PLUS strict que la simple
    visibilite (un participant de conversation peut LIRE un document sans
    pouvoir le supprimer)."""
    with _db() as conn:
        row = conn.execute("SELECT owner_user_id FROM documents WHERE id = %s", (document_id,)).fetchone()
    return bool(row) and row["owner_user_id"] == user_id


def delete_document(document_id: str) -> bool:
    """Cascade geree par Postgres (document_chunks.document_id ON DELETE
    CASCADE, mission §4e) : une seule suppression suffit a rendre le
    document et tous ses chunks immediatement introuvables, y compris pour
    le RAG (evalue a la requete, jamais un cache a invalider)."""
    with _db() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _mark_status(document_id: str, status: str, error: str | None = None) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE documents SET status = %s, status_error = %s, updated_at = now() WHERE id = %s",
            (status, error, document_id),
        )


def _fetch_source_bytes(file_id: str) -> bytes:
    """Le worker RQ n'a pas acces au volume de stockage (meme contrainte que
    jobs.py::_extract_attachment_text) : recuperation via URL signee."""
    expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
    token = sign_file_token(file_id, expires_at)
    url = f"{FRONTEND_URL}/api/workspace/files/{file_id}/signed?exp={expires_at}&token={token}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def _chunk_text(text: str) -> list[str]:
    """Decoupage simple par fenetre glissante (~1000 caracteres, ~150 de
    recouvrement), en preferant couper sur une frontiere de paragraphe
    quand c'est possible dans la fenetre de recherche -- volontairement pas
    de dependance a tiktoken (comptage exact de tokens non necessaire pour
    un decoupage a taille fixe)."""
    normalized = " ".join(text.split())
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + _CHUNK_SIZE, length)
        if end < length:
            boundary = normalized.rfind(". ", start, end)
            if boundary != -1 and boundary > start + (_CHUNK_SIZE // 2):
                end = boundary + 1
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Un seul appel groupe (l'API OpenAI accepte une liste) plutot qu'un
    appel par chunk -- moins de latence, moins de risque de depassement de
    quota par requete. Cle lue UNIQUEMENT via la variable d'environnement
    Railway OPENAI_API_KEY (jamais saisie par ce code -- voir .env.example),
    jamais cote frontend."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RagError("OPENAI_API_KEY n'est pas configuree.")
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in response.data]


def ingest_document(document_id: str) -> None:
    """Pipeline complet (mission §4b). Ne leve jamais vers l'appelant RQ :
    toute erreur est capturee et se traduit par un statut FAILED explicite
    (jamais un document marque READY avec un index partiel/casse, §9)."""
    document = get_document(document_id)
    if not document:
        _logger.warning("ingest_document: document introuvable (%s)", document_id)
        return
    _mark_status(document_id, "PROCESSING")

    try:
        from librairies import text_extraction

        raw = _fetch_source_bytes(document["fileId"])
        text, error = text_extraction.extract_text(raw, document["name"], document.get("mimeType") or "")
        if error:
            _mark_status(document_id, "FAILED", error[:500])
            return

        chunks = _chunk_text(text or "")
        if not chunks:
            _mark_status(document_id, "FAILED", "aucun contenu exploitable apres extraction")
            return

        embeddings = _embed_texts(chunks)
    except Exception as exc:
        _logger.warning("ingest_document a echoue pour %s", document_id, exc_info=True)
        _mark_status(document_id, "FAILED", f"erreur technique lors de l'indexation ({str(exc)[:200]})")
        return

    use_pgvector = _embedding_column_is_vector()
    with _db() as conn:
        if use_pgvector:
            _ensure_vector_adapter(conn)
        # Reindexation (mission §4f) : jamais un melange ancien/nouveau
        # index -- on jette les anciens chunks avant d'inserer les nouveaux,
        # dans la MEME transaction que le passage a READY plus bas.
        conn.execute("DELETE FROM document_chunks WHERE document_id = %s", (document_id,))
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            embedding_value = list(embedding) if use_pgvector else embedding
            conn.execute(
                "INSERT INTO document_chunks (id, document_id, chunk_index, content, embedding) VALUES (%s,%s,%s,%s,%s)",
                (new_id("chunk"), document_id, index, chunk, embedding_value),
            )
        conn.execute(
            """UPDATE documents
               SET status = 'READY', status_error = NULL, extracted_text = %s,
                   indexed_version = content_version, updated_at = now()
               WHERE id = %s""",
            (text[:200000] if text else None, document_id),
        )


def reindex_document(document_id: str) -> bool:
    """Mission §4f : incremente content_version puis relance le pipeline
    complet -- l'ancien index reste consultable (statut inchange) jusqu'a ce
    que le nouveau soit pret, ingest_document() ne remplacant les chunks et
    le statut qu'une fois tout le nouveau contenu pret (pas de version
    mixte)."""
    with _db() as conn:
        row = conn.execute(
            "UPDATE documents SET content_version = content_version + 1, updated_at = now() WHERE id = %s RETURNING id",
            (document_id,),
        ).fetchone()
    if not row:
        return False
    ingest_document(document_id)
    return True


# ---------------------------------------------------------------------------
# Recuperation (outil MCP search_documents, voir rag_tool_server.py)
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_documents(query: str, user_id: str, top_k: int = 6) -> list[dict]:
    """Filtrage d'autorisation AVANT toute lecture de chunk (§4d, point
    critique) : le WHERE ci-dessous s'applique des la jointure document_chunks
    -> documents, jamais un post-filtrage sur un resultat deja recupere."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        query_embedding = _embed_texts([query])[0]
    except Exception:
        _logger.warning("search_documents: embedding de la requete impossible", exc_info=True)
        return []

    use_pgvector = _embedding_column_is_vector()
    with _db() as conn:
        if use_pgvector:
            _ensure_vector_adapter(conn)
            rows = conn.execute(
                f"""SELECT dc.content, d.name AS document_name, d.id AS document_id,
                           dc.embedding <=> %(query_embedding)s AS distance
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE {_VISIBILITY_SQL}
                    ORDER BY distance ASC
                    LIMIT %(top_k)s""",
                {"user_id": user_id, "query_embedding": query_embedding, "top_k": top_k},
            ).fetchall()
            return [{"content": r["content"], "documentName": r["document_name"], "documentId": r["document_id"]} for r in rows]

        # Repli sans pgvector (§4a) : la clause WHERE limite deja aux
        # documents visibles cote SQL (index btree normal, pas couteux) ;
        # seule la similarite cosinus se fait en Python, sur un ensemble deja
        # filtre par autorisation -- jamais sur le corpus entier.
        candidates = conn.execute(
            f"""SELECT dc.content, dc.embedding, d.name AS document_name, d.id AS document_id
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE {_VISIBILITY_SQL}""",
            {"user_id": user_id},
        ).fetchall()
    scored = [
        (_cosine_similarity(query_embedding, list(row["embedding"] or [])), row)
        for row in candidates
        if row["embedding"]
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"content": row["content"], "documentName": row["document_name"], "documentId": row["document_id"]}
        for _, row in scored[:top_k]
    ]
