"""
sports_provider.py — P6.1 (2026-08-01)
Interface de fournisseur de probabilites sportives.

CONTEXTE : 481/628 marches live etaient rejetes faute de fournisseur de
probabilites sports (strategies sports routees mais SANS source, exclues
du perimetre du scanner via no_probability_provider). Ce module definit
l'interface pluggable ; les fournisseurs REELS (flux de cotes, modeles
calibres) arrivent dans P6.2+ et n'ont qu'a implementer get_probability.

INTERFACE :
  SportsProvider.get_probability(ticker, market_data) -> Optional[float]
    probabilite YES en POURCENT (0-100), ou None si le fournisseur ne
    peut pas evaluer ce marche. Aucune probabilite ne doit etre inventee :
    None = pas d'evaluation honnete possible.

  Methods optionnelles (avec defaut sur la classe de base) :
    has_source() -> bool        : True si le fournisseur peut produire
                                  des probabilites (permet au scanner de
                                  n'elargir son perimetre que quand une
                                  source existe vraiment).
    get_confidence(t, m) -> int : confiance 0-10 (0 = pas de signal).

FOURNISSEURS FOURNIS :
  NoOpSportsProvider      : retourne toujours None (comportement actuel).
  StaticSportsProvider    : probabilites codees en dur (tests / shadow).
  CompositeSportsProvider : chaine de fournisseurs, premier resultat
                            non-None gagne. Un fournisseur en panne ne
                            bloque jamais la chaine.

SELECTION PAR ENVIRONNEMENT :
  SPORTS_PROVIDER=composite|static|noop  (defaut: composite, initialement
  vide => comportement strictement identique a aujourd'hui).
  SPORTS_STATIC_PROBABILITIES=JSON {"KX...": 55.0} pour le static.

Aucune dependance vers le reste du moteur : strategie_router peut
importer ce module sans risque de cycle.
"""

import json
import os
from abc import ABC, abstractmethod
from typing import Callable, Dict, Optional

__all__ = [
    "SportsProvider",
    "NoOpSportsProvider",
    "StaticSportsProvider",
    "CompositeSportsProvider",
    "build_sports_provider",
    "sports_provider_to_callable",
    "PROVIDER_COMPOSITE",
    "PROVIDER_STATIC",
    "PROVIDER_NOOP",
]

PROVIDER_COMPOSITE = "composite"
PROVIDER_STATIC = "static"
PROVIDER_NOOP = "noop"


class SportsProvider(ABC):
    """Interface d'un fournisseur de probabilites YES pour marches sports.

    get_probability retourne la probabilite YES en POURCENT (0-100) ou
    None si le marche ne peut pas etre evalue. La probabilite doit etre
    calibree : aucun seuil n'est assoupli pour accommoder un fournisseur
    peu fiable (la confiance le filtre en aval).
    """

    name: str = "base"

    @abstractmethod
    def get_probability(self, ticker: str,
                        market_data: dict) -> Optional[float]:
        """Probabilite YES (0-100) ou None. ticker = Kalshi ticker."""
        raise NotImplementedError

    def has_source(self) -> bool:
        """True si ce fournisseur peut reellement produire une
        probabilite. Le scanner n'elargit son perimetre que si une source
        existe (evite de telecharger des marches condamnes a un rejet
        no_model_probability)."""
        return True

    def get_confidence(self, ticker: str, market_data: dict) -> int:
        """Confiance 0-10. 0 = aucun signal de confiance (rejete par la
        porte MIN_MODEL_CONFIDENCE). Surcharger pour fournir un signal."""
        return 0

    def __repr__(self):  # pragma: no cover - aide au debug
        return f"<{type(self).__name__} name={self.name}>"


class NoOpSportsProvider(SportsProvider):
    """Comportement actuel : aucune donnee sports -> toujours None."""

    name = PROVIDER_NOOP

    def get_probability(self, ticker: str,
                        market_data: dict) -> Optional[float]:
        return None

    def has_source(self) -> bool:
        return False


class StaticSportsProvider(SportsProvider):
    """Probabilites codees en dur (tests, shadow, fixtures).

    probabilities : {ticker: prob_yes_pct}. Un ticker absent retombe sur
    default_probability si fourni, sinon None.
    """

    name = PROVIDER_STATIC

    def __init__(self, probabilities: Optional[Dict[str, float]] = None,
                 default_probability: Optional[float] = None,
                 default_confidence: int = 6):
        self.probabilities: Dict[str, float] = dict(probabilities or {})
        self.default_probability = default_probability
        self.default_confidence = int(default_confidence or 0)

    def get_probability(self, ticker: str,
                        market_data: dict) -> Optional[float]:
        if ticker in self.probabilities:
            return self.probabilities[ticker]
        return self.default_probability

    def get_confidence(self, ticker: str, market_data: dict) -> int:
        return self.default_confidence

    def has_source(self) -> bool:
        return bool(self.probabilities) or self.default_probability is not None

    @classmethod
    def from_env(cls) -> "StaticSportsProvider":
        """Lit SPORTS_STATIC_PROBABILITIES (JSON {ticker: pct}). Valeur
        illisible -> fournisseur vide (aucune source, comportement no-op)."""
        probs: Dict[str, float] = {}
        raw = os.getenv("SPORTS_STATIC_PROBABILITIES", "").strip()
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    probs = {str(k): float(v) for k, v in loaded.items()}
            except (ValueError, TypeError):
                probs = {}
        return cls(probs)


class CompositeSportsProvider(SportsProvider):
    """Chaine de fournisseurs : premier resultat non-None fait foi.

    - add(provider) enregistre un fournisseur supplementaire ;
    - un fournisseur qui leve une exception est IGNORE (la chaine ne doit
      jamais tomber a cause d'une source en panne) ;
    - has_source() est vrai des qu'AU MOINS UN membre a une source.
    """

    name = PROVIDER_COMPOSITE

    def __init__(self, providers=None):
        self.providers: list = []
        if providers:
            for p in providers:
                self.add(p)

    def add(self, provider: SportsProvider) -> "CompositeSportsProvider":
        if not isinstance(provider, SportsProvider):
            raise TypeError(
                f"CompositeSportsProvider n'accepte que des SportsProvider "
                f"(recu: {type(provider).__name__})")
        self.providers.append(provider)
        return self

    def __len__(self) -> int:
        return len(self.providers)

    @property
    def name(self) -> str:
        if not self.providers:
            return f"{PROVIDER_COMPOSITE}(vide)"
        return f"{PROVIDER_COMPOSITE}(" + ",".join(
            getattr(p, "name", type(p).__name__) for p in self.providers) + ")"

    def get_probability(self, ticker: str,
                        market_data: dict) -> Optional[float]:
        for p in self.providers:
            try:
                v = p.get_probability(ticker, market_data)
            except Exception:                       # noqa: BLE001
                continue                            # source en panne = pas bloquant
            if v is not None:
                return v
        return None

    def get_confidence(self, ticker: str, market_data: dict) -> int:
        # confiance du premier fournisseur qui repond
        for p in self.providers:
            try:
                if p.get_probability(ticker, market_data) is not None:
                    return int(getattr(p, "get_confidence", None)(
                        ticker, market_data) if hasattr(p, "get_confidence")
                        else 0) or 0
            except Exception:                       # noqa: BLE001
                continue
        return 0

    def has_source(self) -> bool:
        return any(
            getattr(p, "has_source", lambda: True)()
            for p in self.providers)


def build_sports_provider(name: Optional[str] = None) -> SportsProvider:
    """Fabrique de fournisseur par nom (env SPORTS_PROVIDER par defaut).

    - "" / "composite" / "default" -> CompositeSportsProvider() vide :
      comportement actuel (aucune source, aucun changement) ;
    - "noop" / "none" / "off"      -> NoOpSportsProvider() ;
    - "static"                     -> StaticSportsProvider.from_env().
    Nom inconnu -> ValueError (echec explicite, pas de silence).
    """
    if name is None:
        name = os.getenv("SPORTS_PROVIDER", "")
    name = str(name or "").strip().lower()
    if name in ("", "default", PROVIDER_COMPOSITE):
        return CompositeSportsProvider()
    if name in (PROVIDER_NOOP, "none", "off"):
        return NoOpSportsProvider()
    if name == PROVIDER_STATIC:
        return StaticSportsProvider.from_env()
    raise ValueError(
        f"SPORTS_PROVIDER inconnu: {name!r} "
        f"(valeurs: {PROVIDER_COMPOSITE}|{PROVIDER_STATIC}|{PROVIDER_NOOP})")


def sports_provider_to_callable(provider: SportsProvider) -> Callable:
    """Adapte un SportsProvider a l'interface leguee du StrategyRouter :
    (market, book, minutes_remaining) -> (probability_yes 0-1, confidence)
    ou None si le fournisseur ne peut pas evaluer.

    Conversion : pourcent (0-100) -> fraction (0-1) ; probabilite hors
    (0,1) ou non numerique -> None (rejete honnetement en aval)."""

    def _provider(market: dict, book: dict,
                  minutes_remaining: Optional[float]):
        m = market or {}
        ticker = str(m.get("ticker") or "")
        p = provider.get_probability(ticker, m)
        if p is None:
            return None
        try:
            p = float(p) / 100.0
        except (TypeError, ValueError):
            return None
        if not (0.0 < p < 1.0):
            return None
        conf = provider.get_confidence(ticker, m)
        return (p, int(conf or 0))

    return _provider
