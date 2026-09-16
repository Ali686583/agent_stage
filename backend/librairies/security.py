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
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

_hasher = PasswordHasher()


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
