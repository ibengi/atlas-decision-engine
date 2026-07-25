"""
strategy_router.py — v2 (2026-07-24)
Registre canonique de strategies INDEXE PAR market_type + portes edge/EV.

CAUSE RACINE CORRIGEE : la v1 n'enregistrait qu'une seule strategie
(BtcModelStrategy) dont supports() n'acceptait QUE la serie KXBTC15M —
serie ABSENTE de l'univers scanne (0 occurrence dans les logs). Resultat :
strategy_supported=0 et no_compatible_strategy pour 100 % des candidats,
y compris KXBTCD (btc_above_strike_daily) pourtant compatible.

v2 :
  - registre indexe par market_type (jamais par serie ni par categorie
    generale) ;
  - validate_strategy_registry() : ECHEC DE DEMARRAGE si le registre est
    vide ou si un market_type requis/active n'est pas servi ;
  - strategies sports (moneyline/spread/total) et election_winner
    ENREGISTREES pour le routage. HONNETETE : sans fournisseur de
    probabilite calibre injecte, elles repondent no_model_probability —
    aucune probabilite n'est inventee, aucun seuil n'est assoupli ;
  - strategies BTC (15m et daily) branchees sur btc_probability_model +
    btc_context : probabilite reelle, refusee si donnees insuffisantes ;
  - le nom exact de la strategie choisie est journalise dans chaque
    decision (champ strategy).
"""

import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Callable

from market_classifier import classify, SPORTS_TYPES  # noqa: F401

log = logging.getLogger("ROUTER")

ROUTER_VERSION = "router-v2-2026-07-24"

# market_type qui DOIVENT etre servis par le registre par defaut.
REQUIRED_MARKET_TYPES = ("sports_moneyline", "sports_spread", "sports_total",
                         "btc_above_strike_daily", "election_winner")


# ── Frais (partage avec le backtest : MEME formule) ─────────────────────────

def estimated_fee_per_contract(price_cents: int, fee_rate: float = 0.07) -> float:
    """Frais estime en $ par contrat : ceil(rate*P*(1-P)*100)/100.
    A recaler sur fee_source=api apres les premiers fills reels."""
    p = max(1, min(99, int(price_cents))) / 100.0
    return math.ceil(fee_rate * p * (1 - p) * 100) / 100.0


# ── Configuration des portes ────────────────────────────────────────────────

@dataclass
class GateConfig:
    MIN_MODEL_CONFIDENCE: int = 6
    MIN_GROSS_EDGE: float = 0.05
    MIN_NET_EDGE: float = 0.03
    MIN_NET_EV: float = 0.02
    MAX_ACCEPTABLE_SPREAD: int = 4
    MIN_MARKET_SCORE: float = 50.0
    MIN_FILL_PROXY: float = 40.0
    SLIPPAGE_BUFFER_CENTS: int = 1
    FEE_RATE: float = 0.07
    UNCERTAINTY_BUFFER: float = 0.01
    MAX_ENTRY_CENTS: int = 85
    MAX_DATA_AGE_S: float = 90.0


# ── Sorties de modele et decisions ──────────────────────────────────────────

@dataclass
class ModelOutput:
    valid: bool
    reason: str = ""
    probability_yes: Optional[float] = None
    confidence: int = 0
    features: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Decision:
    ticker: str
    accepted: bool = False
    rejection_reason: Optional[str] = None
    strategy: Optional[str] = None
    market_type: Optional[str] = None
    category: Optional[str] = None
    side: Optional[str] = None            # "yes" | "no"
    entry_ask: Optional[int] = None       # ASK du cote achete (jamais last)
    model_probability: Optional[float] = None   # prob. du cote choisi
    market_probability: Optional[float] = None  # ask/100 du cote choisi
    gross_edge: Optional[float] = None
    net_edge: Optional[float] = None
    net_ev: Optional[float] = None
    confidence: int = 0
    taille: str = "0.5%"
    reason: str = ""
    estimated_fees: Optional[float] = None
    expected_slippage: Optional[float] = None
    model_output: Optional[dict] = None
    decision_id: Optional[str] = None
    spread: Optional[int] = None
    liquidity: Optional[float] = None
    kelly_fraction: Optional[float] = None

    def to_dict(self):
        return asdict(self)


# ── Base de strategie ───────────────────────────────────────────────────────

class Strategy:
    """Interface. market_types : tuple de market_type servis (canonique).
    evaluate() retourne TOUJOURS un ModelOutput ; valid=False + reason si
    la probabilite ne peut pas etre calculee honnetement."""
    name = "base"
    market_types: tuple = ()

    def evaluate(self, market: dict, book: dict,
                 minutes_remaining: Optional[float]) -> ModelOutput:
        raise NotImplementedError

    def has_probability_source(self) -> bool:
        """True si la strategie peut REELLEMENT produire une probabilite
        (modele interne ou fournisseur injecte). Une strategie routable
        mais sans source est exclue du perimetre du scanner : inutile de
        telecharger des marches qui seront rejetes no_model_probability."""
        return True

    def provider_desc(self) -> str:
        """Description humaine de la source de probabilite (audit)."""
        return "modele interne"


class RegistryValidationError(RuntimeError):
    pass


class StrategyRouter:
    def __init__(self):
        self._by_type: dict = {}      # market_type -> Strategy

    def register(self, strategy: Strategy):
        if not strategy.market_types:
            raise RegistryValidationError(
                f"strategie '{strategy.name}' sans market_types — refusee")
        for mt in strategy.market_types:
            if mt in self._by_type:
                raise RegistryValidationError(
                    f"market_type '{mt}' deja servi par "
                    f"'{self._by_type[mt].name}' — doublon interdit "
                    f"(tentative: '{strategy.name}')")
            self._by_type[mt] = strategy
        log.info(f"[REGISTRY] '{strategy.name}' -> "
                 f"{', '.join(strategy.market_types)}")
        return self

    def route(self, market_type: str) -> Optional[Strategy]:
        """Routage par market_type canonique UNIQUEMENT. Ne depend jamais
        d'une categorie generale (qui peut etre erronee)."""
        return self._by_type.get(market_type)

    def tradeable_market_types(self) -> tuple:
        """market_types dont la strategie possede une source de
        probabilite. Sous-ensemble de supported_market_types()."""
        return tuple(sorted(mt for mt, st in self._by_type.items()
                            if st.has_probability_source()))

    def supported_market_types(self) -> tuple:
        return tuple(sorted(self._by_type))

    def registry_matrix(self) -> dict:
        """Matrice market_type -> nom de strategie (livrable 4)."""
        return {mt: s.name for mt, s in sorted(self._by_type.items())}

    def validate_strategy_registry(
            self, required: tuple = REQUIRED_MARKET_TYPES) -> dict:
        """ECHOUE (exception) si le registre est vide ou si un market_type
        requis n'est pas servi. A appeler AU DEMARRAGE : le programme ne
        doit pas tourner avec un registre incoherent."""
        if not self._by_type:
            raise RegistryValidationError(
                "registre de strategies VIDE — demarrage refuse")
        missing = [mt for mt in (required or ()) if mt not in self._by_type]
        if missing:
            raise RegistryValidationError(
                "market_type requis sans strategie enregistree: "
                + ", ".join(missing) + " — demarrage refuse")
        matrix = self.registry_matrix()
        log.info(f"[REGISTRY] validation OK — matrice: {matrix}")
        return matrix


# fonction module-niveau demandee par le cahier des charges
def validate_strategy_registry(router: StrategyRouter,
                               required: tuple = REQUIRED_MARKET_TYPES) -> dict:
    return router.validate_strategy_registry(required)


# ── Utilitaires ─────────────────────────────────────────────────────────────

def minutes_to_close(market: dict, now: Optional[datetime] = None) -> Optional[float]:
    ct = (market or {}).get("close_time")
    if not ct:
        return None
    try:
        close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    return (close_dt - now).total_seconds() / 60.0


def _strike_of(market: dict) -> Optional[float]:
    for k in ("floor_strike", "cap_strike", "strike_price"):
        v = (market or {}).get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# ── Strategies BTC (probabilite REELLE via modele + contexte) ───────────────

class _BtcAboveStrikeBase(Strategy):
    """Base commune BTC 'au-dessus du strike'. context_provider est
    INJECTABLE (tests hors-ligne) ; par defaut btc_context.get_btc_context.
    Refuse toute evaluation si le contexte est invalide ou la qualite de
    donnees insuffisante — rien n'est invente."""

    max_minutes: float = 15.0 * 24 * 60   # surcharge par sous-classe

    def __init__(self, context_provider: Callable = None):
        if context_provider is None:
            try:
                from btc_context import get_btc_context
                context_provider = get_btc_context
            except ImportError:
                context_provider = None
        self._ctx = context_provider

    def evaluate(self, market, book, minutes_remaining) -> ModelOutput:
        from btc_probability_model import (probability_yes,
                                           confidence_from_quality,
                                           ModelInputError, MODEL_VERSION)
        if self._ctx is None:
            return ModelOutput(False, "no_model_probability:btc_context_absent")
        strike = _strike_of(market)
        if strike is None:
            return ModelOutput(False, "no_model_probability:strike_absent")
        t = minutes_remaining
        if t is None or t <= 0:
            return ModelOutput(False, "no_model_probability:horizon_invalide")
        if t > self.max_minutes:
            return ModelOutput(False, "no_model_probability:horizon_hors_perimetre")
        ctx = self._ctx(strike=strike, minutes_remaining=t)
        if ctx is None or not getattr(ctx, "valid", False):
            return ModelOutput(False, "no_model_probability:"
                               + str(getattr(ctx, "reason", "contexte_invalide")))
        conf = confidence_from_quality(ctx.data_quality_score, t)
        if conf <= 0:
            return ModelOutput(False, "insufficient_data_quality:"
                               f"score={ctx.data_quality_score}")
        try:
            p = probability_yes(ctx.spot, strike, ctx.realized_vol_1m, t,
                                ret_5m=(ctx.returns or {}).get("5m"))
        except ModelInputError as e:
            return ModelOutput(False, f"no_model_probability:{e}")
        return ModelOutput(True, "ok", probability_yes=p, confidence=conf,
                           features={"model_version": MODEL_VERSION,
                                     "spot": ctx.spot, "strike": strike,
                                     "sigma_1m": ctx.realized_vol_1m,
                                     "minutes_remaining": t,
                                     "ret_5m": (ctx.returns or {}).get("5m"),
                                     "data_quality": ctx.data_quality_score})


class BtcModelStrategy(_BtcAboveStrikeBase):
    """BTC 15 minutes (serie KXBTC15M, market_type btc_15m_above_strike)."""
    name = "btc15m_above_strike"
    market_types = ("btc_15m_above_strike",)
    max_minutes = 20.0


class BtcDailyStrategy(_BtcAboveStrikeBase):
    """BTC quotidien (serie KXBTCD, market_type btc_above_strike_daily).
    MEME modele, horizon jusqu'a 26 h ; la confiance est reduite au-dela
    de 24 h (voir confidence_from_quality)."""
    name = "btc_daily_above_strike"
    market_types = ("btc_above_strike_daily",)
    max_minutes = 26.0 * 60


# ── Strategies routees SANS modele par defaut (honnetete) ───────────────────

class ProviderBackedStrategy(Strategy):
    """Strategie dont la probabilite vient d'un fournisseur externe
    calibre (ex.: flux de cotes sportives). SANS fournisseur injecte :
    no_model_probability — le routage fonctionne (strategy_supported > 0),
    mais AUCUNE probabilite n'est inventee et aucun trade n'est possible.
    C'est le comportement exige : ne pas assouplir pour forcer des trades."""

    def __init__(self, probability_provider: Callable = None):
        # probability_provider(market, book, minutes_remaining)
        #   -> (probability_yes, confidence) ou None
        self._provider = probability_provider

    def has_probability_source(self) -> bool:
        return self._provider is not None

    def provider_desc(self) -> str:
        if self._provider is None:
            return "AUCUN fournisseur injecte (provider indisponible)"
        return getattr(self._provider, "__name__", "fournisseur externe")

    def evaluate(self, market, book, minutes_remaining) -> ModelOutput:
        if self._provider is None:
            return ModelOutput(
                False, "no_model_probability:aucun_fournisseur_calibre")
        try:
            out = self._provider(market, book, minutes_remaining)
        except Exception as e:                      # fournisseur defaillant
            return ModelOutput(False, f"no_model_probability:fournisseur:{e}")
        if not out:
            return ModelOutput(False,
                               "no_model_probability:fournisseur_sans_reponse")
        p, conf = out
        if p is None or not (0.0 < float(p) < 1.0):
            return ModelOutput(False, "no_model_probability:prob_non_calibree")
        return ModelOutput(True, "ok", probability_yes=float(p),
                           confidence=int(conf or 0),
                           features={"provider": "externe"})


class SportsMoneylineStrategy(ProviderBackedStrategy):
    name = "sports_moneyline_v1"
    market_types = ("sports_moneyline",)


class SportsSpreadStrategy(ProviderBackedStrategy):
    name = "sports_spread_v1"
    market_types = ("sports_spread",)


class SportsTotalStrategy(ProviderBackedStrategy):
    name = "sports_total_v1"
    market_types = ("sports_total",)


class ElectionWinnerStrategy(ProviderBackedStrategy):
    name = "election_winner_v1"
    market_types = ("election_winner",)


# ── Construction du registre par defaut ─────────────────────────────────────

def build_default_registry(btc_context_provider: Callable = None,
                           sports_provider: Callable = None,
                           election_provider: Callable = None,
                           btc_enabled: bool = True) -> StrategyRouter:
    """Registre canonique. Leve RegistryValidationError au moindre
    probleme (doublon, registre vide, market_type requis manquant)."""
    r = StrategyRouter()
    if btc_enabled:
        r.register(BtcModelStrategy(btc_context_provider))
        r.register(BtcDailyStrategy(btc_context_provider))
    r.register(SportsMoneylineStrategy(sports_provider))
    r.register(SportsSpreadStrategy(sports_provider))
    r.register(SportsTotalStrategy(sports_provider))
    r.register(ElectionWinnerStrategy(election_provider))
    r.validate_strategy_registry(REQUIRED_MARKET_TYPES
                                 if btc_enabled else
                                 tuple(t for t in REQUIRED_MARKET_TYPES
                                       if not t.startswith("btc")))
    return r


# ── Portes edge/EV (prix EXECUTABLE : ask a l'achat, jamais last_price) ────

def price_and_gate(dec: Decision, model: ModelOutput, book: dict,
                   gates: GateConfig, market: dict = None) -> Decision:
    """Choisit le cote, calcule edge brut/net et EV net au prix ASK du cote
    achete, applique toutes les portes. Modifie et retourne dec."""
    if not model.valid:
        dec.rejection_reason = model.reason
        return dec
    p_yes = model.probability_yes
    dec.confidence = model.confidence
    dec.model_output = model.to_dict()
    if model.confidence < gates.MIN_MODEL_CONFIDENCE:
        dec.rejection_reason = (f"low_model_confidence:"
                                f"{model.confidence}<{gates.MIN_MODEL_CONFIDENCE}")
        return dec
    spread = book.get("spread")
    dec.spread = spread
    for _k in ("volume_24h", "volume", "open_interest"):
        try:
            dec.liquidity = max(dec.liquidity or 0.0,
                                float((market or {}).get(_k) or 0))
        except (TypeError, ValueError):
            pass
    if spread is not None and spread > gates.MAX_ACCEPTABLE_SPREAD:
        dec.rejection_reason = f"spread_too_wide:{spread}"
        return dec

    best = None
    for side, p_model in (("yes", p_yes), ("no", 1.0 - p_yes)):
        ask = book.get(f"{side}_ask")
        if ask is None or not (1 <= int(ask) <= 99):
            continue                                 # pas de prix executable
        ask = int(ask)
        if ask > gates.MAX_ENTRY_CENTS:
            continue
        mkt_p = ask / 100.0
        gross = p_model - mkt_p
        if gross <= 0:
            continue
        fee = estimated_fee_per_contract(ask, gates.FEE_RATE)
        slip = gates.SLIPPAGE_BUFFER_CENTS / 100.0
        net_edge = gross - fee - slip - gates.UNCERTAINTY_BUFFER
        net_ev = p_model * (1 - mkt_p) - (1 - p_model) * mkt_p - fee - slip
        cand = (net_edge, side, ask, p_model, mkt_p, gross, fee, slip, net_ev)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        dec.rejection_reason = "no_positive_edge"
        return dec
    net_edge, side, ask, p_model, mkt_p, gross, fee, slip, net_ev = best
    dec.side, dec.entry_ask = side, ask
    dec.model_probability, dec.market_probability = p_model, mkt_p
    dec.gross_edge, dec.net_edge, dec.net_ev = gross, net_edge, net_ev
    dec.estimated_fees, dec.expected_slippage = fee, slip
    # Kelly binaire informatif (b = gain net/mise) — DIAGNOSTIC uniquement,
    # le sizing reel reste le PositionSizer (% capital effectif).
    b = (100 - ask) / ask
    dec.kelly_fraction = round(max(0.0, p_model - (1 - p_model) / b), 4)
    if gross < gates.MIN_GROSS_EDGE:
        dec.rejection_reason = f"insufficient_gross_edge:{gross:.4f}"
        return dec
    if net_edge < gates.MIN_NET_EDGE:
        dec.rejection_reason = f"insufficient_net_edge:{net_edge:.4f}"
        return dec
    if net_ev <= gates.MIN_NET_EV:
        dec.rejection_reason = f"negative_net_ev:{net_ev:.4f}"
        return dec
    dec.accepted = True
    dec.taille = "1%" if dec.confidence >= 8 else "0.5%"
    dec.reason = (f"{dec.strategy}: p={p_model:.3f} vs ask={ask}c "
                  f"edge_net={net_edge:+.3f} ev_net={net_ev:+.3f}")
    return dec
