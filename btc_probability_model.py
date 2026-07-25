"""
btc_probability_model.py — v1-ref (2026-07-24)
Modele de probabilite BTC "au-dessus du strike" a l'horizon T minutes.

ATTENTION (honnetete) : le fichier original n'a pas ete fourni dans les
livrables recus ; cette version de REFERENCE implemente exactement le
contrat consomme par backtest_btc15m.py (norm_cdf, MODEL_VERSION,
MOMENTUM_CAP) et par les strategies BTC du routeur. Si une version
anterieure existe dans le depot, comparer les constantes avant fusion.

Modele : marche brownien geometrique sans drift (mu=0 par defaut),
  d = ln(spot/strike) / (sigma_1m * sqrt(T))
  P(YES) = Phi(d + momentum_ajuste)
avec un ajustement de momentum optionnel borne par MOMENTUM_CAP
(momentum = (ret_5m / 5) * T / (sigma_1m * sqrt(T))), identique au
backtest — donc backtest et production utilisent LA MEME formule.

La probabilite N'EST CALCULEE QUE si toutes les entrees indispensables
sont presentes et valides. Aucune valeur par defaut n'est inventee.
"""

import math

MODEL_VERSION = "btc15m-v1.0-ref"
MOMENTUM_CAP = 0.5          # borne de l'ajustement momentum (en unites de d)
P_FLOOR, P_CEIL = 0.0001, 0.9999


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class ModelInputError(ValueError):
    """Entree indispensable absente ou invalide — aucune probabilite."""


def _require_pos(name, v):
    if v is None or not isinstance(v, (int, float)) \
            or math.isnan(v) or math.isinf(v) or v <= 0:
        raise ModelInputError(f"{name}_invalide({v!r})")
    return float(v)


def probability_yes(spot: float, strike: float, sigma_1m: float,
                    minutes_remaining: float, ret_5m: float = None,
                    use_momentum: bool = True) -> float:
    """P(spot_final > strike). Leve ModelInputError si une donnee
    indispensable manque : RIEN n'est suppose."""
    spot = _require_pos("spot", spot)
    strike = _require_pos("strike", strike)
    sigma = _require_pos("sigma_1m", sigma_1m)
    t = _require_pos("minutes_remaining", minutes_remaining)
    denom = sigma * math.sqrt(t)
    if denom <= 0:
        raise ModelInputError("denominateur_nul")
    d = math.log(spot / strike) / denom
    mu = 0.0
    if use_momentum and ret_5m is not None \
            and isinstance(ret_5m, (int, float)) \
            and not (math.isnan(ret_5m) or math.isinf(ret_5m)):
        mu = max(-MOMENTUM_CAP,
                 min(MOMENTUM_CAP, (float(ret_5m) / 5.0) * t / denom))
    return min(P_CEIL, max(P_FLOOR, norm_cdf(d + mu)))


def confidence_from_quality(data_quality_score: float,
                            minutes_remaining: float) -> int:
    """Confiance 0..10 derivee de la qualite de donnees mesuree par
    btc_context (0..100) et de l'horizon. Deterministe et documente :
      - < 60 de qualite -> 0 (le routeur rejettera insufficient_data_quality)
      - sinon base = qualite/10, -1 si l'horizon depasse 24 h (sigma 1m
        extrapole loin de sa fenetre de mesure)."""
    try:
        q = float(data_quality_score)
    except (TypeError, ValueError):
        return 0
    if q < 60.0:
        return 0
    base = int(q // 10)
    if minutes_remaining is not None and minutes_remaining > 24 * 60:
        base -= 1
    return max(0, min(10, base))
