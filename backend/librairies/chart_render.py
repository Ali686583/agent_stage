"""
librairies/chart_render.py
=============================

Rendu REEL des courbes/graphiques en Python + Matplotlib (jamais Chart.js
cote client, jamais un graphique invente) : l'IA (ChatGPT/Claude, via n8n)
insere une specification JSON de graphique directement dans son texte,
sous forme d'un bloc de code balise ```chart_spec``` ; ce module la
retrouve, la valide, genere une vraie image PNG avec matplotlib (backend
"Agg", sans affichage -- jamais besoin d'un environnement graphique), et
la restitue en base64. Voir librairies/jobs.py::_split_markdown_into_blocks,
qui appelle ce module puis reconstruit la liste de blocs du message en
respectant EXACTEMENT l'ordre texte/graphique original -- jamais deplace
au debut ou a la fin.

CHART_POLICY est injectee dans CHAQUE prompt envoye a l'IA (voir jobs.py,
meme principe que PLATFORM_DATA_POLICY dans n8n_client.py) : decrit le
format exact attendu, insiste sur "jamais de donnees inventees", et precise
qu'aucun graphique ne doit etre ajoute si ni la demande ni le bouton/
workflow selectionne n'en ont besoin.

Une specification invalide ou un type de graphique non supporte n'est
JAMAIS silencieusement ignoree ou remplacee par un graphique invente : le
texte original (bloc de code inclus) est laisse tel quel dans le message,
et l'erreur est seulement journalisee cote serveur.
"""

from __future__ import annotations

import base64
import io
import logging
import re

import matplotlib

matplotlib.use("Agg")  # jamais d'environnement graphique sur le serveur
import matplotlib.pyplot as plt  # noqa: E402

_logger = logging.getLogger(__name__)

# Balise choisie pour ne jamais entrer en collision avec un bloc de code
# "python"/"json" legitime que l'IA voudrait montrer par ailleurs.
CHART_SPEC_FENCE_RE = re.compile(r"```chart_spec\s*\n(.*?)\n```", re.DOTALL)

SUPPORTED_CHART_TYPES = {"line", "bar", "pie", "scatter", "histogram"}

MAX_SERIES = 12
MAX_POINTS_PER_SERIES = 500

_PALETTE = ["#0F6722", "#6DB831", "#d97706", "#2563eb", "#9333ea", "#dc2626", "#0891b2", "#be185d"]

CHART_POLICY = (
    "CHART POLICY: charts/graphs in this application are rendered server-side with real "
    "Python + Matplotlib -- never describe a chart in words instead of generating one, and "
    "never claim to have attached an image without following this exact mechanism. To include "
    "a chart, insert a fenced code block tagged exactly ```chart_spec``` (own line) containing "
    "ONLY a single JSON object, placed EXACTLY where the chart should appear relative to your "
    "explanatory text (before/after text stays in the same order you wrote it -- never moved to "
    "the start or end). You may include multiple such blocks in one response, each exactly where "
    "that chart belongs. JSON schema: "
    '{"chartType": "line"|"bar"|"scatter"|"histogram"|"pie", "title": "optional string", '
    '"xLabel": "optional string", "yLabel": "optional string", '
    '"series": [{"label": "optional string", "x": [...], "y": [...]}]} '
    '-- for "pie" use instead: {"chartType": "pie", "title": "optional", '
    '"labels": [...], "values": [...]} -- for "histogram" each series uses "values": [...] '
    "(raw numbers to bin) instead of x/y. "
    "Use ONLY real data actually available to you (attachment, connected platform/API, MCP "
    "tool result, database, n8n workflow output, or conversation context) -- NEVER invent or "
    "estimate data points for a chart. If a chart would need data you don't actually have, do "
    "not fabricate one -- say so in text instead. Only include a chart when your own request "
    "analysis, or the selected button/workflow's task, genuinely calls for one -- never add one "
    "just because it's possible."
)


class ChartSpecError(ValueError):
    """Specification de graphique invalide ou type non supporte -- jamais
    levee au-dela de ce module (voir _try_render, qui la catch toujours)."""


def _validate_series(series, require_xy: bool) -> list[dict]:
    if not isinstance(series, list) or not series:
        raise ChartSpecError("series manquantes ou vides")
    if len(series) > MAX_SERIES:
        raise ChartSpecError("trop de series")
    cleaned = []
    for serie in series:
        if not isinstance(serie, dict):
            raise ChartSpecError("serie invalide")
        label = str(serie.get("label") or "")[:100]
        if require_xy:
            x = serie.get("x")
            y = serie.get("y")
            if not isinstance(x, list) or not isinstance(y, list) or len(x) != len(y) or not x:
                raise ChartSpecError("x/y manquants, vides ou de longueurs differentes")
            if len(x) > MAX_POINTS_PER_SERIES:
                raise ChartSpecError("trop de points")
            cleaned.append({"label": label, "x": x, "y": y})
        else:
            values = serie.get("values")
            if not isinstance(values, list) or not values:
                raise ChartSpecError("values manquantes ou vides")
            if len(values) > MAX_POINTS_PER_SERIES:
                raise ChartSpecError("trop de points")
            cleaned.append({"label": label, "values": values})
    return cleaned


def _render_figure(spec: dict):
    chart_type = spec.get("chartType")
    if chart_type not in SUPPORTED_CHART_TYPES:
        raise ChartSpecError(f"type de graphique non supporte: {chart_type}")

    title = str(spec.get("title") or "")[:150]
    x_label = str(spec.get("xLabel") or "")[:80]
    y_label = str(spec.get("yLabel") or "")[:80]

    fig, ax = plt.subplots(figsize=(7, 4), dpi=150)

    if chart_type == "pie":
        labels = spec.get("labels")
        values = spec.get("values")
        if not isinstance(labels, list) or not isinstance(values, list) or len(labels) != len(values) or not values:
            raise ChartSpecError("labels/values manquants, vides ou de longueurs differentes pour un pie chart")
        if len(values) > MAX_SERIES * 4:
            raise ChartSpecError("trop de parts")
        ax.pie(values, labels=[str(v)[:60] for v in labels], colors=_PALETTE, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
    elif chart_type == "histogram":
        series = _validate_series(spec.get("series"), require_xy=False)
        for i, serie in enumerate(series):
            ax.hist(serie["values"], bins=20, alpha=0.7, label=serie["label"] or None, color=_PALETTE[i % len(_PALETTE)])
        if any(s["label"] for s in series):
            ax.legend()
        ax.set_xlabel(x_label or None)
        ax.set_ylabel(y_label or "Frequence")
    else:
        series = _validate_series(spec.get("series"), require_xy=True)
        for i, serie in enumerate(series):
            color = _PALETTE[i % len(_PALETTE)]
            label = serie["label"] or None
            if chart_type == "line":
                ax.plot(serie["x"], serie["y"], marker="o", color=color, label=label, linewidth=2)
            elif chart_type == "bar":
                width = 0.8 / len(series)
                offsets = [pos + i * width - (0.8 - width) / 2 for pos in range(len(serie["x"]))]
                ax.bar(offsets, serie["y"], width=width, color=color, label=label)
                if i == 0:
                    ax.set_xticks(range(len(serie["x"])))
                    ax.set_xticklabels([str(v) for v in serie["x"]], rotation=30, ha="right")
            elif chart_type == "scatter":
                ax.scatter(serie["x"], serie["y"], color=color, label=label)
        if any(s["label"] for s in series):
            ax.legend()
        ax.set_xlabel(x_label or None)
        ax.set_ylabel(y_label or None)

    if title:
        ax.set_title(title)
    fig.tight_layout()
    return fig


def _fig_to_data_uri(fig) -> str:
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png")
    finally:
        plt.close(fig)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def split_markdown_into_blocks(content: str) -> list[dict]:
    """Coupe un texte markdown en une sequence ordonnee de blocs
    {"type": "markdown", "content": ...} et {"type": "image", "url": ...}
    (meme contrat que le bloc "image" deja gere par renderImageBlock cote
    frontend -- une data URI base64 est une valeur "url" valide, jamais
    besoin d'un nouveau type de bloc) a chaque occurrence d'un
    ```chart_spec``` valide -- l'ordre texte/
    graphique exact du contenu original est toujours respecte (jamais
    deplace au debut/a la fin, voir CHART_POLICY). Une specification
    invalide n'est jamais silencieusement supprimee : le bloc de code
    d'origine reste tel quel dans le texte, comme si aucun rendu n'avait
    ete tente. Renvoie toujours au moins un bloc markdown (eventuellement
    vide) si aucun chart_spec valide n'est trouve, pour ne jamais changer
    le contrat existant (un seul bloc markdown) quand cette fonctionnalite
    n'est pas utilisee."""
    blocks: list[dict] = []
    last_end = 0
    for match in CHART_SPEC_FENCE_RE.finditer(content):
        data_uri = render_chart_spec(match.group(1))
        if data_uri is None:
            continue  # specification invalide : laisse le texte original tel quel
        text_before = content[last_end:match.start()].strip("\n")
        if text_before.strip():
            blocks.append({"type": "markdown", "content": text_before})
        blocks.append({"type": "image", "url": data_uri, "alt": "Graphique"})
        last_end = match.end()
    remaining = content[last_end:].strip("\n")
    if remaining.strip() or not blocks:
        blocks.append({"type": "markdown", "content": remaining})
    return blocks


def render_chart_spec(raw_json: str) -> str | None:
    """Renvoie une data URI PNG, ou None si la specification est invalide
    (jamais d'exception qui remonterait -- voir jobs.py, qui laisse alors le
    texte original tel quel plutot que de casser toute la reponse)."""
    import json

    try:
        spec = json.loads(raw_json)
    except (ValueError, TypeError) as exc:
        _logger.info("chart_spec JSON invalide: %s", exc)
        return None
    if not isinstance(spec, dict):
        _logger.info("chart_spec n'est pas un objet JSON")
        return None
    try:
        fig = _render_figure(spec)
    except ChartSpecError as exc:
        _logger.info("chart_spec rejete: %s", exc)
        return None
    except Exception:
        _logger.exception("Erreur inattendue lors du rendu d'un chart_spec")
        return None
    try:
        return _fig_to_data_uri(fig)
    except Exception:
        _logger.exception("Erreur inattendue lors de l'encodage d'un chart_spec")
        return None
