"""
librairies/rag_jobs.py
========================

Point d'entree RQ pour l'ingestion RAG (mission RAG §4c). File d'attente
SEPAREE de "agent_stage_workflows" (voir librairies/jobs.py) : l'indexation
d'une piece jointe ne doit jamais retarder une reponse ChatGPT/Claude en
attente sur la meme file. Le worker Railway existant doit ecouter les DEUX
files (voir worker.py, mise a jour -- aucun nouveau service Railway requis).
"""

from __future__ import annotations

import logging
import os

import redis
from rq import Queue

from librairies import rag

_logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL n'est pas configuree : voir librairies/rag_jobs.py.")

_redis = redis.Redis.from_url(REDIS_URL)
rag_queue = Queue("agent_stage_rag", connection=_redis)


def run_ingestion(document_id: str) -> None:
    """Execute par le worker RQ. Ne leve jamais : toute erreur est deja
    capturee dans rag.ingest_document (statut FAILED persiste), rien a
    remonter de plus ici."""
    rag.ingest_document(document_id)


def enqueue_ingestion(document_id: str) -> None:
    rag_queue.enqueue(run_ingestion, document_id)
