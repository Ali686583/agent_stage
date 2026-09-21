# NOTICE — Composants tiers

Ce projet (voir LICENSE à la racine du dépôt pour la notice de copyright
du code propriétaire original) utilise les composants tiers open source
suivants. Chacun reste régi par sa propre licence, telle que publiée par
son mainteneur respectif — cette notice ne fait que les recenser
(attribution), elle ne remplace ni ne modifie leurs licences d'origine.

## Backend (Python — voir backend/requirements.txt)

- Flask — https://github.com/pallets/flask
- gunicorn — https://github.com/benoitc/gunicorn
- Flask-Limiter — https://github.com/alisaifee/flask-limiter
- argon2-cffi — https://github.com/hynek/argon2-cffi
- psycopg — https://github.com/psycopg/psycopg
- requests — https://github.com/psf/requests
- Pillow — https://github.com/python-pillow/Pillow
- redis-py — https://github.com/redis/redis-py
- rq — https://github.com/rq/rq
- cryptography — https://github.com/pyca/cryptography

## Frontend (chargés depuis cdnjs.cloudflare.com, voir backend/frontend/page2.html)

- Chart.js — https://github.com/chartjs/Chart.js
- marked — https://github.com/markedjs/marked
- DOMPurify — https://github.com/cure53/DOMPurify
- jsPDF — https://github.com/parallax/jsPDF
- jsPDF-AutoTable — https://github.com/simonbengtsson/jsPDF-AutoTable

## Infrastructure tierce (services, pas du code distribué avec ce dépôt)

- Railway (hébergement) — https://railway.com
- n8n (orchestration des workflows) — https://n8n.io
- PostgreSQL, Redis — logiciels tiers utilisés en tant que services gérés,
  non distribués avec ce dépôt.

Aucune attribution requise par l'une de ces licences n'est supprimée ou
remplacée par la notice de copyright de LICENSE, qui ne concerne que le
code propriétaire original de ce dépôt.
