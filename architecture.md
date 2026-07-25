# Architecture

## Flux d'un cycle

```
MarketScanner ──▶ classification ──▶ filtres immédiats ──▶ plafond/cycle
     │  (cache univers ≥30 min,        (expiré, fermé,       (tri tradabilité)
     │   séries ciblées)                sans liquidité,
     ▼                                  type inconnu)
OpportunityPipeline
     │  cache de rejets TTL ─▶ StrategyRouter.route(market_type)
     │                              │
     │                              ▼ Strategy.evaluate → ModelOutput
     │                         price_and_gate (ask, frais, slippage,
     │                              tampon, spread, confiance,
     │                              net_edge > seuil, net_ev > 0)
     ▼
ExecutionEngine.cycle
     │  kill switch → can_trade (stop jour %, pertes consécutives,
     │  drawdown) → capital effectif = min(solde, plafond)
     │  → budgets (total 5 %, catégorie, marché) → PositionSizer (≤1 %)
     ▼
OrderManager.place_and_track  (client_order_id idempotent, TTL,
     │                         annulation si non rempli,
     │                         fills confirmés via l'API uniquement)
     ▼
TradeLogger + PositionManager (règlement sur `result` API,
                               réconciliation broker au démarrage)
```

## Modules

| Dossier | Fichier | Rôle |
|---|---|---|
| src/engine | kalshi_alpha_bot.py | Config, client API, ordres, positions, risque, sizing, moteur, CLI |
| src/engine | opportunity_pipeline.py | Funnel de décision, résumé de cycle, decisions.jsonl |
| src/strategies | strategy_router.py | Registre canonique, validation au démarrage, portes de prix |
| src/scanner | market_scanner.py / market_classifier.py / market_ranker.py | Univers, classification, score |
| src/ai | btc_probability_model.py, btc_context.py, model_calibration.py, shadow_prediction_store.py, model_gatekeeper.py | Probabilités, données marché, calibration, shadow, verrou LIVE |
| src/dashboard | status.py | Statut CLI |

## Invariants
1. Aucun ordre sans prix limite 1–99 et book non vide côté visé.
2. Aucun trade enregistré sans fill confirmé par l'endpoint fills.
3. Aucune limite de risque fondée sur un capital supérieur au solde.
4. `strategy_supported` compté uniquement si une stratégie route ET accepte
   le périmètre (horizon, type) du marché.
5. Un seul résumé structuré par cycle ; détails en JSONL rotatif.
