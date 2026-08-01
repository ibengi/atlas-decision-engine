#!/usr/bin/env python3
"""Configuration centralisee du logging de l'Atlas Decision Engine (P4.1).

Variables d'environnement :
  LOG_FORMAT  json (defaut) -> une ligne JSON par evenement sur stdout
              text          -> format lisible humainement (developpement)
  LOG_LEVEL   niveau de log (INFO par defaut)

Le formateur est applique au logger racine : tous les canaux (BOT, API,
TRADE, RISK, POSITION, STATS, SCANNER, PIPELINE, ...) propagent vers la
racine, donc un seul handler suffit. Les appels log.* existants sont
inchanges — les donnees contextuelles supplementaires sont passees via
extra={...} et apparaissent comme champs du JSON.
"""

import logging
import os
import sys

from json_formatter import JsonFormatter

TEXT_FORMAT = "%(asctime)s  %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_MARKER = "_atlas_structured_handler"


def make_formatter(fmt=None) -> logging.Formatter:
    """Construit le formateur selon LOG_FORMAT (defaut: json)."""
    fmt = (fmt or os.getenv("LOG_FORMAT", "json")).strip().lower()
    if fmt == "json":
        return JsonFormatter()
    if fmt == "text":
        return logging.Formatter(TEXT_FORMAT, datefmt=DATE_FORMAT)
    raise ValueError(
        f"LOG_FORMAT inconnu: {fmt!r} (valeurs acceptees: json, text)")


def setup_logging(level=None, fmt=None, stream=None) -> logging.Handler:
    """Configure le logging du moteur sur stdout (idempotent).

    - JSON-lines par defaut (LOG_FORMAT=json), texte lisible si
      LOG_FORMAT=text.
    - Semantique identique a l'ancien logging.basicConfig : si un hote a
      deja configure le logger racine (ex: pytest), on ne touche a rien —
      le moteur n'ecrase jamais la configuration de son environnement.
    - Si notre handler est deja pose, on rafraichit format et niveau
      (appels repetes / changement de LOG_FORMAT en cours de vie).
    """
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    stream = stream if stream is not None else sys.stdout
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, _HANDLER_MARKER, False):
            h.setFormatter(make_formatter(fmt))
            root.setLevel(level)
            return h
    if root.handlers:          # hote deja configure : parity basicConfig
        return None
    handler = logging.StreamHandler(stream)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(make_formatter(fmt))
    root.setLevel(level)
    root.addHandler(handler)
    return handler
