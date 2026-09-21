"""
librairies/security.py
=======================

Primitives de securite du backend agent_stage :
  - hachage des mots de passe (Argon2id)
  - generation et hachage des tokens opaques (sessions, reset de mot de passe)

Les mots de passe ne sont jamais stockes ni logues en clair. Le hash Argon2id
encode son propre sel et ses parametres, donc aucune colonne "salt" separee
n'est necessaire (contrairement a l'ancien schema PBKDF2 de MyBusiness).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

_hasher = PasswordHasher()

FILE_SIGNING_SECRET = os.environ.get("FILE_SIGNING_SECRET", "")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHash:
        return True


def generate_token(n_bytes: int = 32) -> str:
    """Token opaque, url-safe, cryptographiquement aleatoire."""
    return secrets.token_urlsafe(n_bytes)


def hash_token(token: str) -> str:
    """Empreinte SHA-256 d'un token, stockee en base a la place du token brut.

    Le token brut ne circule que dans le cookie / le lien envoye par email ;
    seule son empreinte est persistee, pour qu'une fuite de la base ne
    permette pas de rejouer une session ou une reinitialisation.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_file_token(file_id: str, expires_at: int) -> str:
    """Partagee entre le process web (workspace_routes.py, pour construire ET
    verifier le lien) et le worker en arriere-plan (librairies/jobs.py, pour
    construire le lien transmis a n8n) : les deux doivent produire exactement
    la meme signature a partir du meme secret."""
    if not FILE_SIGNING_SECRET:
        raise RuntimeError("FILE_SIGNING_SECRET n'est pas configuree.")
    message = f"{file_id}:{expires_at}".encode("utf-8")
    return hmac.new(FILE_SIGNING_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_tool_token(resource: str, expires_at: int) -> str:
    """Meme principe et meme secret que sign_file_token ci-dessus, generalise
    a une ressource interne quelconque (ex : l'URL SSE de l'outil de
    recherche web transmise a n8n, voir librairies/jobs.py et
    librairies/web_search_tool_server.py) -- jamais une URL "outil" ouverte
    sans signature sur l'internet public."""
    if not FILE_SIGNING_SECRET:
        raise RuntimeError("FILE_SIGNING_SECRET n'est pas configuree.")
    message = f"{resource}:{expires_at}".encode("utf-8")
    return hmac.new(FILE_SIGNING_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
