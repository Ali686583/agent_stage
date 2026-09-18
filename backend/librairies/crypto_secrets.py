"""
librairies/crypto_secrets.py
==============================

Chiffrement REVERSIBLE des clefs API des connexions plateformes/API
(banque de connexions, voir librairies/connections_bank.py, Postgres-jg_R).

Different des primitives existantes de librairies/security.py : les mots de
passe (Argon2id) et les tokens de session/reset (SHA-256) sont hashes a
SENS UNIQUE, jamais redechiffres. Une clef API de plateforme externe doit au
contraire pouvoir etre relue cote serveur pour etre transmise a cette
plateforme (voir librairies/platform_client.py) : on utilise donc Fernet
(AES-128-CBC + HMAC, package `cryptography`) plutot qu'un hachage.

CONNECTION_ENCRYPTION_KEY est fournie par l'environnement Railway (jamais
codee en dur, jamais journalisee, jamais renvoyee dans une reponse API) :
une cle Fernet valide (32 octets, urlsafe-base64 -- generable via
`python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Si elle manque ou est invalide, on leve une erreur de configuration
explicite plutot que d'echouer silencieusement ou de stocker en clair.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

CONNECTION_ENCRYPTION_KEY = os.environ.get("CONNECTION_ENCRYPTION_KEY", "")


class SecretConfigError(RuntimeError):
    """Levee quand CONNECTION_ENCRYPTION_KEY manque/est invalide, ou quand
    un secret ne peut pas etre dechiffre (cle changee entre-temps)."""


def _fernet() -> Fernet:
    if not CONNECTION_ENCRYPTION_KEY:
        raise SecretConfigError(
            "CONNECTION_ENCRYPTION_KEY n'est pas configuree : voir librairies/crypto_secrets.py."
        )
    try:
        return Fernet(CONNECTION_ENCRYPTION_KEY.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise SecretConfigError("CONNECTION_ENCRYPTION_KEY est invalide.") from exc


def encrypt_secret(plaintext: str) -> str:
    """Chiffre une clef API avant stockage. Jamais appele avec une valeur
    vide : voir la validation dans connections_bank.create_connection."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Dechiffre une clef API stockee. Reserve a un usage strictement
    interne (librairies/platform_client.py) : jamais expose via une route."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretConfigError("Impossible de dechiffrer cette connexion (cle changee ?).") from exc
