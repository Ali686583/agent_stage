"""
librairies/realtime.py
========================

Transport temps reel (Phase 3) : Redis Pub/Sub, utilise uniquement comme
relais ephemere entre les workers Gunicorn qui tiennent une connexion SSE
ouverte. Ce module ne persiste jamais rien lui-meme : PostgreSQL (via
librairies/workspace.py) reste l'unique source de verite. Si Redis est
indisponible, l'ecriture d'un message ne doit jamais echouer pour autant
(voir l'appel best-effort dans workspace.py::add_message) ; seul le push
temps reel est perdu, le message reste recuperable au prochain rattrapage.
"""

from __future__ import annotations

import json
import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "")

if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL n'est pas configuree : le temps reel ne peut pas demarrer "
        "(voir librairies/realtime.py)."
    )

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _channel(conversation_id: str) -> str:
    return f"conv:{conversation_id}:events"


def publish_event(conversation_id: str, event_type: str, data: dict) -> None:
    _client.publish(_channel(conversation_id), json.dumps({"type": event_type, "data": data}))


def subscribe(conversation_id: str):
    """Renvoie un objet pubsub deja abonne. L'appelant doit toujours faire
    pubsub.close() (dans un finally) quand le client SSE se deconnecte."""
    pubsub = _client.pubsub()
    pubsub.subscribe(_channel(conversation_id))
    return pubsub


# ---------------------------------------------------------------------------
# Canal PAR UTILISATEUR (mission notifications, Phase 6) : distinct du canal
# par conversation ci-dessus -- une notification doit atteindre son
# destinataire quelle que soit la conversation actuellement ouverte dans son
# navigateur (y compris aucune). Meme mecanisme, meme tolerance aux pannes
# (best-effort, jamais une notification perdue cote Postgres si Redis est
# indisponible -- voir workspace.add_message, qui persiste TOUJOURS la
# notification avant de tenter cette publication).
# ---------------------------------------------------------------------------

def _user_channel(user_id: str) -> str:
    return f"user:{user_id}:notifications"


def publish_user_event(user_id: str, event_type: str, data: dict) -> None:
    _client.publish(_user_channel(user_id), json.dumps({"type": event_type, "data": data}))


def subscribe_user(user_id: str):
    """Meme contrat que subscribe() ci-dessus : l'appelant doit toujours
    fermer le pubsub a la deconnexion du flux SSE."""
    pubsub = _client.pubsub()
    pubsub.subscribe(_user_channel(user_id))
    return pubsub
