# Roadmap

## Fait (v12.1)
- [x] Diagnostic complet par decision (raison finale unique) + Kelly informatif.
- [x] Perimetre du scanner = types avec source de probabilite
      (`tradeable_market_types`), exclusions `no_compatible_strategy` /
      `no_probability_provider` des le scanner.
- [x] Classification grossiere deterministe par categorie API (plus
      d'`unknown` pour un marche identifiable).
- [x] Funnel avec % de conversion ; trace d'ordre Preparing → Payload →
      Response → Order ID → Fill ; refus API avec HTTP status + body.
- [x] `--stats` : Win Rate, PnL, ROI, Profit Factor, Expectancy, Edge moyen,
      duree moyenne, Sharpe/trade, Max Drawdown (avec drapeau low_sample).

## Court terme
- [ ] Validation shadow 24 h+ en continu et calibration mesurée de
      qualité→confiance (≥ 300 prédictions réglées).
- [ ] Vérification des champs API réels via `[RAW:*]` (frais, avg_price,
      curseurs par série) après premier déploiement.
- [ ] Revue périodique de `DEFAULT_SERIES_BY_MARKET_TYPE` (Kalshi ajoute
      des séries).

## Moyen terme
- [ ] Fournisseur de probabilités sports (cotes de clôture) branché sur
      `ProviderBackedStrategy`, validé en shadow avant activation.
- [ ] Fournisseur élections (agrégateur de sondages) — même exigence.
- [ ] Modèle de slippage fondé sur la profondeur du carnet (remplace le
      tampon constant).
- [ ] Dashboard web (lecture seule) remplaçant `src/dashboard/status.py`.

## Long terme
- [ ] Verrou distribué pour le multi-instances.
- [ ] Calibration continue (recalcul Platt glissant sur le shadow store).
- [ ] Support d'autres classes de marchés (météo, éco) avec la même règle :
      pas de modèle calibré → pas de trade.

## Non-objectifs
- Auto-promotion en LIVE. Jamais.
- Assouplir des seuils pour générer du volume.
