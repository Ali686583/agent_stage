"""
librairies/platform_client.py
===============================

Decide QUELLES connexions plateformes/API selectionnees sont pertinentes
pour une demande donnee (prompt §17-22 : selectionnee != automatiquement
utilisee), puis recupere reellement les donnees des connexions jugees
pertinentes.

LIMITE HONNETE (a signaler, pas a masquer) : ce projet n'a aujourd'hui
AUCUNE integration specifique deja construite avec une plateforme externe
(pas de Google Drive, pas de CRM, pas d'API Finance reelle). Le seul
adaptateur cable ici est un client REST generique (GET authentifie par
cle porteuse dans un en-tete), suffisant pour toute API qui accepte une
cle API simple dans un en-tete. Brancher une integration reellement
specifique (OAuth Google Drive, format de reponse d'un CRM precis, etc.)
demanderait son propre adaptateur -- non fabrique ici pour ne pas simuler
une integration qui n'existe pas reellement.

Pertinence : approche par mots-cles (prompt §42 : "évite les conditions
codées en dur du type if platform === X" -- ici, aucune plateforme n'est
jamais nommee en dur, la pertinence est entierement pilotee par les
`keywords` renseignes a la creation de CHAQUE connexion, en base). Ce
n'est pas une comprehension semantique du langage naturel (qui demanderait
un appel a un modele de langage supplementaire, avec son propre cout/
latence, non demande explicitement) : c'est une correspondance textuelle
simple entre la demande (+ les actions selectionnees) et ces mots-cles.
"""

from __future__ import annotations

import requests


def is_relevant(connection: dict, haystack: str) -> bool:
    haystack_lower = (haystack or "").lower()
    for keyword in connection.get("keywords") or []:
        if keyword and keyword.lower() in haystack_lower:
            return True
    return False


def select_relevant_connections(
    connections: list[dict], entry_text: str, action_names: list[str]
) -> list[dict]:
    """RÈGLE ABSOLUE (prompt §9/10/21) : une connexion selectionnee mais non
    pertinente n'est jamais appelee. Evalue chaque connexion independamment
    (prompt §22)."""
    haystack = " ".join([entry_text or "", *action_names])
    return [c for c in connections if is_relevant(c, haystack)]


def fetch_platform_data(connection: dict, api_key: str, timeout: int = 10) -> tuple[dict | None, str | None]:
    """Appelle la plateforme via un GET authentifie generique. Ne leve
    JAMAIS : une source secondaire indisponible ne doit pas faire echouer
    toute la demande (prompt §45), l'appelant recoit (None, erreur) et
    continue sans cette source."""
    config = connection.get("config") or {}
    base_url = config.get("baseUrl")
    if not base_url:
        return None, "no_base_url_configured"
    try:
        response = requests.get(
            base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:4000]}
        return data, None
    except requests.exceptions.RequestException as exc:
        return None, str(exc)[:300]
