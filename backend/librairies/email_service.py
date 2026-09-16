"""
librairies/email_service.py
============================

Envoi de l'email de reinitialisation de mot de passe via SMTP (Gmail par
defaut). Toutes les informations de connexion viennent des variables
d'environnement Railway : aucun identifiant n'est ecrit dans le code.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD ne sont pas configures.")

    message = EmailMessage()
    message["Subject"] = "Reinitialisation de votre mot de passe"
    message["From"] = SMTP_FROM
    message["To"] = to_email
    message.set_content(
        "Bonjour,\n\n"
        "Une demande de reinitialisation de mot de passe a ete faite pour ce "
        "compte.\n\n"
        f"Pour choisir un nouveau mot de passe, ouvrez ce lien :\n{reset_link}\n\n"
        "Ce lien expire dans un temps limite et ne peut etre utilise qu'une "
        "seule fois.\n\n"
        "Si vous n'etes pas a l'origine de cette demande, ignorez simplement "
        "cet email : aucune modification ne sera effectuee."
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)
