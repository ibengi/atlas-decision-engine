#!/usr/bin/env python3
"""JsonFormatter — sortie des logs en JSON-lines (Phase 4.1, P4.1).

Chaque evenement logue devient un objet JSON sur une seule ligne, ce qui
permet l'agregation, la recherche et le monitoring (Logstash/Datadog/...).
Champs standards : timestamp (ISO 8601 UTC), level, logger, message, module.
Toutes les donnees contextuelles passees via extra={...} sont conservees
comme champs supplementaires du JSON (ex: ticker, side, price, size, edge).

Extrait de la strategie P4.1 (Observability) du roadmap :
  - Replace logging.basicConfig avec un logger JSON-lines
  - Conserve les memes noms de canaux (BOT, API, TRADE, RISK, ...)
  - LOG_FORMAT=json (defaut) / LOG_FORMAT=text (fallback dev lisible)
"""

import json
import logging
from datetime import datetime, timezone

# Attributs internes du LogRecord (et ceux calcules par le formatter) qui
# ne doivent JAMAIS etre exposes comme champs du JSON : ils sont soit
# representes par un champ standard, soit inutiles/risques (chemins locaux,
# numeros de thread, ...).
_RESERVED_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class JsonFormatter(logging.Formatter):
    """Formateur qui produit une ligne JSON par evenement logue.

    Usage:
        handler.setFormatter(JsonFormatter())
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
        }
        # Champs contextuels supplementaires (extra={...}) — tout ce qui
        # n'est pas un attribut interne du LogRecord est preserve tel quel.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        # default=str : jamais d'exception de serialisation (Decimal, objets
        # non-standards passes en extra), le JSON reste toujours valide.
        return json.dumps(payload, ensure_ascii=False, default=str)
