"""
librairies/email_service.py
============================

Envoi de l'email de reinitialisation de mot de passe via l'API HTTPS de
Resend (https://resend.com).

Railway bloque les ports SMTP sortants (25/465/587) sur les plans
Free/Trial/Hobby : un envoi par SMTP classique (Gmail, etc.) echoue toujours
avec "Network is unreachable" depuis ce service, quel que soit le code.
Resend contourne le probleme en envoyant par une simple requete HTTPS.

Toutes les informations de connexion viennent des variables d'environnement
Railway : aucune cle n'est ecrite dans le code.
"""

from __future__ import annotations

import os

import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
RESEND_API_URL = "https://api.resend.com/emails"


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY n'est pas configuree.")

    response = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM,
            "to": [to_email],
            "subject": "Reinitialisation de votre mot de passe",
            "text": (
                "Bonjour,\n\n"
                "Une demande de reinitialisation de mot de passe a ete faite "
                "pour ce compte.\n\n"
                f"Pour choisir un nouveau mot de passe, ouvrez ce lien :\n{reset_link}\n\n"
                "Ce lien expire dans un temps limite et ne peut etre utilise "
                "qu'une seule fois.\n\n"
                "Si vous n'etes pas a l'origine de cette demande, ignorez "
                "simplement cet email : aucune modification ne sera effectuee."
            ),
        },
        timeout=15,
    )
    response.raise_for_status()
