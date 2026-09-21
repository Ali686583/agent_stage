"""
librairies/web_search.py
==========================

Veille web par mots-cles, pour deux boutons integres distincts (voir
workflow_bank.py) : "Veille Web (sans API)" (flux RSS public de Google
Actualites, actualites uniquement) et "Veille Web (recherche generale)"
(resultats web generaux via DuckDuckGo, sans cle API, sans compte).

Aucune des deux sources ne demande de compte, de cle ou de carte bancaire :
choix delibere suite a la decision de ne pas utiliser un service payant
(Tavily) qui demandait une carte bancaire meme sur son offre gratuite.

LEGALITE (verifiee avant d'ecrire ce module, jamais supposee) : les deux
sources ne portent QUE sur des pages/articles PUBLICS, jamais une recherche
sur une personne identifiee.
  - Le flux RSS Google Actualites est un canal de syndication publiquement
    documente, sans authentification, concu pour la consommation
    programmatique (different d'un scraping de la page HTML de resultats
    Google, que Google interdit explicitement hors de son API officielle).
  - La page de resultats HTML de DuckDuckGo (html.duckduckgo.com) n'a pas
    d'equivalent officiel a une politique d'interdiction comme celle de
    Google ; sa lecture automatisee est une pratique tres repandue (utilisee
    par de nombreux outils open-source de recherche pour agents IA). Risque
    juridique plus faible qu'un scraping Google, mais moins formellement
    "sanctionne" qu'un flux RSS officiel -- transparence assumee plutot que
    presentee comme une certitude absolue.

N'invente jamais un resultat : toute source indisponible, vide ou en erreur
remonte un code d'erreur stable (voir WebSearchError) plutot qu'un contenu
fabrique -- voir librairies/jobs.py, qui traduit ce code pour l'utilisateur.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlparse

import requests

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
# Identifie honnetement l'appelant (jamais un User-Agent de navigateur usurpe) :
# bonne pratique de scraping, et evite d'etre confondu avec un vrai utilisateur.
DUCKDUCKGO_USER_AGENT = "agent-stage-veille-web/1.0 (+https://backend-production-7bf3.up.railway.app)"

MAX_RESULTS = 8
MAX_QUERY_LENGTH = 300


class WebSearchError(RuntimeError):
    """Code d'erreur stable (jamais un texte libre) : voir jobs.py, qui le
    traduit pour l'utilisateur -- jamais une trace brute ou un secret affiche."""


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


class _DuckDuckGoResultParser(HTMLParser):
    """Extraction minimale (titre/lien/extrait) de la page de resultats HTML
    "lite" de DuckDuckGo : pas de dependance externe (BeautifulSoup n'est
    pas dans requirements.txt), juste le parseur HTML de la bibliotheque
    standard. Best-effort : une structure HTML modifiee cote DuckDuckGo fait
    au pire remonter une liste vide (donc "no_results"), jamais une erreur
    ni un contenu invente."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._capture: str | None = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        href = dict(attrs).get("href") or ""
        if tag == "a" and "result__a" in classes:
            self.results.append({"title": "", "url": _unwrap_duckduckgo_redirect(href), "snippet": ""})
            self._capture = "title"
        elif "result__snippet" in classes:
            self._capture = "snippet"

    def handle_data(self, data):
        if self._capture and self.results:
            self.results[-1][self._capture] += data

    def handle_endtag(self, tag):
        if tag in ("a", "div", "h2"):
            self._capture = None


def _unwrap_duckduckgo_redirect(href: str) -> str:
    """DuckDuckGo enveloppe chaque lien dans une redirection interne
    (/l/?uddg=<url encodee>&...) : on en extrait l'URL reelle pour ne jamais
    renvoyer un lien de redirection opaque a l'utilisateur/l'IA."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path == "/l/":
        real_url = parse_qs(parsed.query).get("uddg")
        if real_url:
            return real_url[0]
    return href


def search_public_web_general(keywords: str) -> list[dict]:
    """Recherche generale (pas seulement des actualites) sur des pages
    publiques via DuckDuckGo, sans cle API ni compte -- voir la section
    LEGALITE en tete de module."""
    query = _clean_query(keywords)
    try:
        response = requests.post(
            DUCKDUCKGO_HTML_URL,
            data={"q": query},
            headers={"User-Agent": DUCKDUCKGO_USER_AGENT},
            timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise WebSearchError("network_error") from exc

    parser = _DuckDuckGoResultParser()
    try:
        parser.feed(response.text)
    except Exception as exc:
        raise WebSearchError("network_error") from exc

    results = []
    for item in parser.results[:MAX_RESULTS]:
        title = " ".join(item["title"].split())
        if not title:
            continue
        results.append(
            {
                "title": title,
                "url": item["url"],
                "publishedAt": "",
                "source": "",
                "snippet": " ".join(item["snippet"].split()),
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
