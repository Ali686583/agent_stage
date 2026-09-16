"""
librairies/email_service.py
============================

Envoi de l'email de reinitialisation de mot de passe via l'API HTTPS de
Brevo (https://www.brevo.com).

Railway bloque les ports SMTP sortants (25/465/587) sur les plans
Free/Trial/Hobby : un envoi par SMTP classique (Gmail, etc.) echoue toujours
avec "Network is unreachable" depuis ce service, quel que soit le code.
Brevo contourne le probleme en envoyant par une simple requete HTTPS, et
autorise l'envoi vers n'importe quel destinataire des qu'un expediteur est
verifie (contrairement au mode sandbox de Resend, limite a l'adresse du
compte tant qu'aucun domaine n'est verifie).

Toutes les informations de connexion viennent des variables d'environnement
Railway : aucune cle n'est ecrite dans le code.
"""

from __future__ import annotations

import os

import requests

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME", "agent_stage")
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise RuntimeError("BREVO_API_KEY / BREVO_SENDER_EMAIL ne sont pas configures.")

    response = requests.post(
        BREVO_API_URL,
        headers={
            "api-key": BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "sender": {"email": BREVO_SENDER_EMAIL, "name": BREVO_SENDER_NAME},
            "to": [{"email": to_email}],
            "subject": "Reinitialisation de votre mot de passe",
            "textContent": (
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
