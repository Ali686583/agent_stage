"""
librairies/rate_limit.py
=========================

Instance Flask-Limiter partagee entre server.py (routes d'authentification)
et workspace_routes.py (espace collaboratif). Vit dans son propre module
pour que les deux puissent l'importer sans dependance circulaire entre eux.
"""

from __future__ import annotations

from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def client_ip() -> str:
    # Railway termine les connexions via son propre edge et expose l'IP
    # reelle du client dans X-Real-Ip (X-Forwarded-For seul est trompeur
    # derriere ce proxy, voir server.py).
    return request.headers.get("X-Real-Ip") or get_remote_address()


limiter = Limiter(client_ip, default_limits=[], storage_uri="memory://")
