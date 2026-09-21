"""
librairies/oauth_connector.py
================================

Connecteur OAuth2 GENERIQUE pour l'entree 2 de la banque de connexions
plateformes/API (voir librairies/connections_bank.py). Contrairement a
librairies/google_drive.py (integration figee, un seul fournisseur -- Google
-- avec un jeton personnel par UTILISATEUR), ce module ne connait AUCUN
fournisseur particulier : chaque connexion fournit ses propres URLs
d'autorisation/de jeton + son propre client_id/client_secret, obtenus par
l'utilisateur sur la console developpeur de la plateforme visee (CB
Insights, Salesforce, Microsoft, etc.) -- meme principe que les "connexions
personnalisees" des outils d'automatisation grand public.

Le jeton resultant est stocke au niveau de la CONNEXION (partagee par tout
l'espace collaboratif), pas par utilisateur : coherent avec le reste de la
banque (une connexion creee par quelqu'un est utilisable par tous, voir
connections_bank.py). Toute la persistance (chiffrement inclus) est deleguee
a connections_bank.py -- ce module ne fait QUE la logique HTTP du protocole
OAuth2 "authorization code" standard.

CONNECTIONS_OAUTH_REDIRECT_URI est fournie par l'environnement Railway :
URL de callback FIXE (jamais devinee dynamiquement depuis la requete, pour
ne jamais risquer un redirect_uri qui ne correspond pas exactement a celui
enregistre cote fournisseur -- cause frequente d'echec OAuth), a enregistrer
par l'utilisateur sur la console developpeur de CHAQUE plateforme externe
qu'il connecte.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote

import requests

from librairies import connections_bank

CONNECTIONS_OAUTH_REDIRECT_URI = os.environ.get("CONNECTIONS_OAUTH_REDIRECT_URI", "")

# Marge de securite : rafraichit un jeton encore valide quelques secondes
# avant son expiration reelle plutot que d'attendre l'echec, pour ne jamais
# risquer d'utiliser un jeton perime pendant l'appel qui suit.
REFRESH_MARGIN_SECONDS = 60


class OAuthConnectorConfigError(RuntimeError):
    """Levee quand CONNECTIONS_OAUTH_REDIRECT_URI n'est pas configuree, ou
    quand la connexion visee n'a pas ses 4 champs OAuth requis (entree 2 non
    remplie ou incomplete)."""


class OAuthConnectorError(RuntimeError):
    """Erreur fonctionnelle (code d'echange refuse, reponse invalide, etc.) :
    code court et stable, jamais un texte libre -- voir workspace_routes.py,
    qui le traduit pour l'utilisateur."""


def is_globally_configured() -> bool:
    return bool(CONNECTIONS_OAUTH_REDIRECT_URI)


def build_authorization_url(connection_id: str, state: str) -> str:
    """Construit l'URL de consentement du fournisseur externe vers laquelle
    rediriger le navigateur. `state` doit etre une valeur imprevisible liee
    a la session en cours (protection CSRF standard du flux OAuth), fournie
    par l'appelant (voir workspace_routes.py)."""
    if not CONNECTIONS_OAUTH_REDIRECT_URI:
        raise OAuthConnectorConfigError("CONNECTIONS_OAUTH_REDIRECT_URI n'est pas configuree.")
    oauth_config = connections_bank.get_oauth_client_config(connection_id)
    if not oauth_config:
        raise OAuthConnectorConfigError("Cette connexion n'a pas de configuration OAuth (entree 2 vide).")
    params = {
        "client_id": oauth_config["clientId"],
        "redirect_uri": CONNECTIONS_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    if oauth_config.get("scope"):
        params["scope"] = oauth_config["scope"]
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    separator = "&" if "?" in oauth_config["authorizeUrl"] else "?"
    return f"{oauth_config['authorizeUrl']}{separator}{query}"


def _post_token_request(token_url: str, data: dict) -> dict:
    try:
        response = requests.post(token_url, data=data, headers={"Accept": "application/json"}, timeout=15)
    except requests.exceptions.RequestException as exc:
        raise OAuthConnectorError("network_error") from exc
    if not response.ok:
        raise OAuthConnectorError("oauth_exchange_failed")
    try:
        return response.json()
    except ValueError as exc:
        raise OAuthConnectorError("oauth_invalid_response") from exc


def exchange_code_and_store(connection_id: str, code: str) -> None:
    """Termine le flux OAuth pour cette connexion : echange le code contre
    un access_token (+ refresh_token si fourni), et les persiste chiffres.
    Leve OAuthConnectorConfigError/OAuthConnectorError -- jamais une fausse
    reussite silencieuse."""
    if not CONNECTIONS_OAUTH_REDIRECT_URI:
        raise OAuthConnectorConfigError("CONNECTIONS_OAUTH_REDIRECT_URI n'est pas configuree.")
    oauth_config = connections_bank.get_oauth_client_config(connection_id)
    if not oauth_config:
        raise OAuthConnectorConfigError("Cette connexion n'a pas de configuration OAuth (entree 2 vide).")
    tokens = _post_token_request(
        oauth_config["tokenUrl"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CONNECTIONS_OAUTH_REDIRECT_URI,
            "client_id": oauth_config["clientId"],
            "client_secret": oauth_config["clientSecret"],
        },
    )
    access_token = tokens.get("access_token")
    if not access_token:
        raise OAuthConnectorError("no_access_token")
    connections_bank.set_oauth_tokens(connection_id, access_token, tokens.get("refresh_token"), tokens.get("expires_in"))


def get_valid_access_token(connection_id: str) -> str | None:
    """Cote worker (librairies/jobs.py) : renvoie un access_token frais pour
    cette connexion, rafraichi automatiquement si expire -- jamais un jeton
    perime silencieusement. None si jamais connectee, ou si le
    rafraichissement echoue (fournisseur ayant revoque l'acces, par
    exemple) : l'appelant doit pouvoir dire clairement que cette source
    n'est pas utilisable, jamais planter toute la demande (meme contrat que
    librairies/platform_client.py)."""
    tokens = connections_bank.get_oauth_tokens(connection_id)
    if not tokens or not tokens.get("accessToken"):
        return None
    expires_at = tokens.get("expiresAt")
    if not expires_at or expires_at - time.time() > REFRESH_MARGIN_SECONDS:
        return tokens["accessToken"]
    if not tokens.get("refreshToken"):
        # Pas de refresh_token (certains fournisseurs n'en renvoient pas) :
        # on continue avec le jeton existant plutot que d'echouer a tort --
        # il pourra encore etre valide cote fournisseur.
        return tokens["accessToken"]
    oauth_config = connections_bank.get_oauth_client_config(connection_id)
    if not oauth_config:
        return None
    try:
        refreshed = _post_token_request(
            oauth_config["tokenUrl"],
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refreshToken"],
                "client_id": oauth_config["clientId"],
                "client_secret": oauth_config["clientSecret"],
            },
        )
    except OAuthConnectorError:
        return None
    new_access_token = refreshed.get("access_token")
    if not new_access_token:
        return None
    connections_bank.set_oauth_tokens(
        connection_id, new_access_token, refreshed.get("refresh_token") or tokens["refreshToken"], refreshed.get("expires_in")
    )
    return new_access_token


def disconnect(connection_id: str) -> bool:
    return connections_bank.clear_oauth_tokens(connection_id)
