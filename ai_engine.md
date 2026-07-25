# Moteur IA

## Modèle BTC (daily + 15 min)
`P(YES) = Φ( ln(spot/strike) / (σ₁ₘ·√t) + momentum_borné )`
- σ₁ₘ : volatilité réalisée 1 min (btc_context, multi-sources spot :
  Coinbase/Kraken/Bitstamp, klines Binance) ;
- momentum : rendement 5 min normalisé, borné par `MOMENTUM_CAP` ;
- refus d'émettre (`ModelInputError` / contexte invalide) si < 2 sources
  spot concordantes, qualité < 60/100, strike ou horizon absents.

## Confiance
`confidence_from_quality(score, t)` ∈ 0–10 ; heuristique documentée, **non
calibrée** — la porte `MIN_MODEL_CONFIDENCE` reste inchangée tant que la
calibration shadow (≥ 300 prédictions réglées) n'est pas mesurée.

## Calibration
`model_calibration.py` : Platt scaling déterministe (a·logit(p)+b),
ajusté sur TRAIN uniquement dans `examples/backtest_btc15m.py`
(découpe chronologique 60/20/20, Brier et log-loss vs baseline marché).

## Shadow
`shadow_prediction_store.py` enregistre chaque probabilité émise et la
règle sur le `result` API — jamais sur une estimation. C'est la source de
vérité pour valider (ou invalider) le modèle avant toute promotion.

## Stratégies sans modèle
Sports et élections passent par `ProviderBackedStrategy` : sans fournisseur
de probabilités calibrées injecté, elles rejettent `no_model_probability`.
Brancher un fournisseur = implémenter `provider(market) -> float ∈ ]0,1[`
et le passer à `build_default_registry(...)`.
