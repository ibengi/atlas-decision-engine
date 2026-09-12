"""Process-local registry of actual Alpha persistence paths. SHADOW ONLY.

A runtime object's custom path is source state even when configuration does
not name it. Registrations survive object disposal and path reconfiguration so
a later report cannot replace an earlier telemetry snapshot.
"""

import os
import threading
from contextlib import contextmanager

_LOCK = threading.RLock()
_PATHS = {}


def register_persistence_path(path, label):
    path = os.path.abspath(os.fspath(path))
    with _LOCK:
        _PATHS[path] = label
    return path


def registered_persistence_paths():
    with _LOCK:
        return dict(_PATHS)


@contextmanager
def publication_guard():
    """Freeze runtime path registration across report's final check/replace."""
    with _LOCK:
        yield
