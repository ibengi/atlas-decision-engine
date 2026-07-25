# Changelog

## [12.2.0] — 2026-07-25
### Ajoute
- Mode EXECUTION_MODE=real_demo avec garde anti-mock FATAL (exit 3) : aucun
  double de test, dry-run ou shadow ne peut remplacer l'appel API reel.
- Banner [EXECUTION] explicite au demarrage (env, URL, mode, dry_run,
  mock_enabled, order_submission_enabled) + mention "fonds DEMO".
- Protocole de preuve d'ordre : ORDER_SUBMIT_ATTEMPT/RESPONSE/FAILED (avec
  http_status reel), ORDER_VERIFY(_FAILED) par relecture immediate,
  ORDER_WAITING_FOR_FILL cadence 2-5 s, ORDER_FILLED / ORDER_CANCELED_
  UNFILLED, FILL_VERIFY, POSITION_VERIFY via /portfolio/positions ;
  POSITION_OPENED emis UNIQUEMENT si la position est retrouvee via l'API.
- Ordre non confirme par relecture => aucun trade enregistre.
- scripts/kalshi_demo_execution_check.py : test d'integration REEL DEMO
  (flag explicite, URL DEMO verifiee, 1 contrat minimal, rapport
  demo_execution_proof.json) — a executer sur Railway avec les vraies cles.
- Config : DRY_RUN, ALLOW_ORDER_SUBMISSION, ORDER_VERIFY_INTERVAL_SECONDS,
  ORDER_FILL_TIMEOUT_SECONDS (alias TTL), CANCEL_UNFILLED_ORDERS.
### Tests
- 95 tests (15 nouveaux), 0 echec.

## [12.1.0] — 2026-07-25
### Ajoute
- Tracabilite par decision : diagnostic complet (categorie, type, strategie,
  liquidite, spread, probabilites, edge/EV nets, Kelly informatif,
  confiance) + raison finale UNIQUE dans decisions.jsonl.
- Trace d'ordre complete : Preparing → Payload JSON → API Response →
  Order ID → fill confirme ; refus API journalises avec HTTP status + body.
- Funnel de cycle avec pourcentages de conversion a chaque etape.
- `python run.py --stats` : statistiques de performance depuis les trades
  regles (drapeau explicite si echantillon < 30).
- `StrategyRouter.tradeable_market_types()` : le scanner ne cible que les
  types dotes d'une source de probabilite ; exclusions explicites
  `no_probability_provider` et `no_compatible_strategy` des le scanner.
- Classification grossiere par categorie API : plus d'`unknown` pour un
  marche identifiable (PGA => Sports, Weather => weather_other, ...).
### Tests
- 80 tests (12 nouveaux), 0 echec.

## [12.0.0] — 2026-07-24
### Corrige
- Registre de strategies canonique indexe par `market_type` ; refus de
  demarrage si registre vide/incomplet (etait : 1 seule strategie limitee a
  une serie absente de l'univers → `strategy_supported=0`).
- Classification deterministe par prefixe de serie (etait : sous-chaines —
  « Las Vegas » → Commodities, « POL » → Politics, etc.).
- Scanner cible + cache univers ≥ 30 min avec rafraichissement incremental
  (etait : 40 001 marches re-telecharges par minute).
- Limites de risque en % du capital effectif = min(solde, plafond) ;
  stop jour 5 %, 1 %/trade, 3 positions max, arret apres 3 pertes
  consecutives (etait : stop fixe −50 $ sur un compte de 93,26 $).
### Ajoute
- 68 tests (classification, routage, scanner, sizing petit compte,
  cycle de vie des ordres, reconciliation, separation DEMO/LIVE).
- `model_gatekeeper` : LIVE impossible sans tests verts, validation modele
  et leve explicite de `NO_LIVE_PROMOTION`.
- Resume de cycle structure unique `[CYCLE-SUMMARY]` + decisions JSONL rotatif.

## [11.x] — historique interne (pre-open-source)
