"""Ajoute les sous-paquets de src/ au sys.path pour que les modules
(historiquement plats) continuent de s'importer entre eux sans refactor
des imports. Idempotent."""
import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def add_src_to_path():
    for d in ("engine", "ai", "strategies", "scanner", "dashboard", "utils"):
        p = os.path.join(_SRC, d)
        if p not in sys.path:
            sys.path.insert(0, p)
