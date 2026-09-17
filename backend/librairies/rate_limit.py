"""
librairies/rate_limit.py
=========================

Instance Flask-Limiter partagee entre server.py (routes d'authentification)
et workspace_routes.py (espace collaboratif). Vit dans son propre module
pour que les deux puissent l'importer sans dependance circulaire entre eux.

Stockage Redis (au lieu de memory://) : necessaire des qu'on tourne avec
plusieurs workers/instances Gunicorn (Phase 2/3), sinon chaque worker aurait
son propre compteur en memoire et la limite reelle serait multipliee par le
nombre de workers. REDIS_URL est obligatoire ; pas de repli silencieux vers
memory:// pour ne pas deployer une limite qui semble marcher mais ne
protege plus rien des qu'il y a plus d'un worker.
"""

from __future__ import annotations

import os

from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

REDIS_URL = os.environ.get("REDIS_URL", "")

if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL n'est pas configuree : le rate limiter ne peut pas demarrer "
        "sans stockage partage (voir librairies/rate_limit.py)."
    )


def client_ip() -> str:
    # Railway termine les connexions via son propre edge et expose l'IP
    # reelle du client dans X-Real-Ip (X-Forwarded-For seul est trompeur
    # derriere ce proxy, voir server.py).
    return request.headers.get("X-Real-Ip") or get_remote_address()


limiter = Limiter(client_ip, default_limits=[], storage_uri=REDIS_URL)
