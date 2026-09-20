"""
librairies/web_search.py
==========================

Veille web par mots-cles, pour deux boutons integres distincts (voir
workflow_bank.py) : "Veille Web (sans API)" (flux RSS public de Google
Actualites, aucune cle requise) et "Veille Web (Tavily)" (API de recherche
tierce, cle API requise).

LEGALITE (verifiee avant d'ecrire ce module, jamais supposee) : les deux
sources ne portent QUE sur des pages/articles PUBLICS, jamais une recherche
sur une personne identifiee. Le flux RSS Google Actualites est un canal de
syndication publiquement documente, sans authentification, concu pour la
consommation programmatique (different d'un scraping de la page HTML de
resultats Google, que Google interdit explicitement hors de son API
officielle). Tavily est une API commerciale dont les conditions d'usage
autorisent explicitement ce cas d'usage (recherche pour agents IA).

N'invente jamais un resultat : toute source indisponible, vide ou en erreur
remonte un code d'erreur stable (voir WebSearchError) plutot qu'un contenu
fabrique -- voir librairies/jobs.py, qui traduit ce code pour l'utilisateur.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"

MAX_RESULTS = 8
MAX_QUERY_LENGTH = 300


class WebSearchError(RuntimeError):
    """Code d'erreur stable (jamais un texte libre) : voir jobs.py, qui le
    traduit pour l'utilisateur -- jamais une trace brute ou un secret affiche."""


def is_tavily_configured() -> bool:
    return bool(TAVILY_API_KEY)


def _clean_query(keywords: str) -> str:
    cleaned = (keywords or "").strip()
    if not cleaned:
        raise WebSearchError("empty_query")
    return cleaned[:MAX_QUERY_LENGTH]


def search_public_news_rss(keywords: str, lang: str = "fr", country: str = "FR") -> list[dict]:
    """Recherche par mots-cles sur le flux RSS PUBLIC de Google Actualites.
    Jamais d'authentification, jamais de scraping HTML : uniquement ce flux
    de syndication documente et stable."""
    query = _clean_query(keywords)
    params = {"q": query, "hl": lang, "gl": country, "ceid": f"{country}:{lang}"}
    query_string = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    try:
        response = requests.get(f"{GOOGLE_NEWS_RSS_URL}?{query_string}", timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise WebSearchError("network_error") from exc
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise WebSearchError("network_error") from exc

    results = []
    for item in root.findall("./channel/item")[:MAX_RESULTS]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source_el = item.find("source")
        results.append(
            {
                "title": title,
                "url": (item.findtext("link") or "").strip(),
                "publishedAt": (item.findtext("pubDate") or "").strip(),
                "source": (source_el.text or "").strip() if source_el is not None else "",
                "snippet": (item.findtext("description") or "").strip(),
            }
        )
    if not results:
        raise WebSearchError("no_results")
    return results


def search_public_web_tavily(keywords: str) -> list[dict]:
    """Recherche par mots-cles via l'API Tavily (necessite TAVILY_API_KEY,
    fournie par l'environnement Railway -- jamais codee en dur)."""
    if not TAVILY_API_KEY:
        raise WebSearchError("not_configured")
    query = _clean_query(keywords)
    try:
        response = requests.post(
            TAVILY_SEARCH_URL,
            json={"api_key": TAVILY_API_KEY, "query": query, "max_results": MAX_RESULTS, "search_depth": "basic"},
            timeout=20,
        )
    except requests.exceptions.RequestException as exc:
        raise WebSearchError("network_error") from exc
    if response.status_code in (401, 403):
        raise WebSearchError("not_configured")
    if not response.ok:
        raise WebSearchError("network_error")
    try:
        data = response.json()
    except ValueError as exc:
        raise WebSearchError("network_error") from exc

    results = []
    for item in (data.get("results") or [])[:MAX_RESULTS]:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        results.append(
            {
                "title": title,
                "url": item.get("url") or "",
                "publishedAt": item.get("published_date") or "",
                "source": "",
                "snippet": (item.get("content") or "")[:500],
            }
        )
    if not results:
        raise WebSearchError("no_results")
    return results


def format_results_for_prompt(results: list[dict]) -> str:
    lines = []
    for idx, result in enumerate(results, start=1):
        meta = " - ".join(p for p in (result.get("source"), result.get("publishedAt")) if p)
        lines.append(f"{idx}. {result['title']}" + (f" ({meta})" if meta else ""))
        if result.get("snippet"):
            lines.append(result["snippet"])
        if result.get("url"):
            lines.append(result["url"])
        lines.append("")
    return "\n".join(lines).strip()
