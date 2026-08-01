"""api_cache.py — utilitaire de cache TTL simple (P8, stdlib uniquement).

Cache a expiration (TTL) thread-safe utilise par les couches reseau du
moteur (KalshiClient, contexte BTC). Aucune dependance : threading + time.

Contrats
--------
- ``get(key)``  : None si absent ou expire (l'entree expiree est purgee).
- ``set(key, v)`` : stocke avec l'horodatage courant.
- ``clear()``   : vide le cache (appele au debut de chaque cycle pour les
  caches "par cycle" — jamais de donnee d'un cycle precedent).
- Toutes les operations sont protegees par un verrou : utilisable depuis
  le thread du moteur et les threads paralleles (P8).

Les valeurs fausses/failback (None, listes vides) ne sont JAMAIS cachees
par les appelants : une erreur API transitoire ne masque pas un retablissement.
"""

import threading
import time


class TTLCache:
    """Cache cle -> valeur avec expiration. TTL en secondes (> 0)."""

    def __init__(self, ttl_s: float = 15.0):
        self.ttl_s = max(0.001, float(ttl_s))
        self._lock = threading.Lock()
        self._data = {}            # key -> (value, ts)

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, ts = entry
            if time.time() - ts >= self.ttl_s:
                del self._data[key]
                return None
            return value

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (value, time.time())

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
