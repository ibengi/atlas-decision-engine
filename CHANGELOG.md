# Changelog

## [12.6.0] — 2026-07-26
### Dashboard web (nouveau)
- src/dashboard/dashboard_web.py : serveur HTTP stdlib (ZERO dependance),
  thread daemon a cote du moteur ; GET / (UI sombre, polling 5 s, canvas)
  et GET /api/state (JSON agrege).
- Donnees 100 % REELLES lues dans les fichiers d'etat : solde/capital
  effectif, courbe d'equity = PnL realise cumule des trades regles,
  P&L/jour, P&L/strategie, candidats du dernier cycle (proba modele vs
  marche, edge/EV, statut dont blocked:*), positions, ordres suivis,
  funnel + rejets, compteur shadow, indicateur cooldown 503.
- HONNETETE d'affichage : « — » quand une donnee n'existe pas ; bandeau
  low_sample tant que n<30 trades regles ; note explicite que le modele
  btc15m-v1.0-ref n'est PAS calibre. Les chiffres de la maquette d'origine
  (WR 66,67 %, Sharpe 1.87, strategies ETH) etaient fictifs et ne sont
  PAS reproduits.
- Moteur : ecrit dashboard_state.json a chaque cycle (candidats, solde,
  cycle, shadow, pause 503) ; DASHBOARD_ENABLED (defaut 1) ; port = $PORT
  Railway sinon DASHBOARD_PORT (8080). Echec du dashboard = warning,
  jamais d'impact trading.
### Tests
- 139 tests (4 nouveaux : agregation honnete, exposition, serveur HTTP),
  0 echec. Demo locale validee sur donnees identiques a la prod.

## [12.5.0] — 2026-07-26
### CRITIQUE — fills reels non comptabilises + re-soumission en boucle
Logs 2026-07-25 23:14+ : ordres V2 crees (201) et REMPLIS, mais
GET /portfolio/orders/{id} repond 404 pour les ordres V2 -> chaque fill
reel etait rejete "unverified", AUCUN trade local, et le meme signal
etait re-soumis chaque cycle (solde 80.21$ -> 74.93$ en ~8 cycles sur le
MEME YES@74c ; reglements gagnants revenus ensuite : 74.67 -> 87.70$).
Correctifs :
- ORDER_VERIFY : si GET ordre repond 404 pour un ordre V2, la reponse de
  creation (moteur d'appariement : order_id, fill_count, remaining_count,
  ts_ms) sert de verification, loggee source=create_response_v2 ;
  confirmation finale inchangee via FILL_VERIFY + POSITION_VERIFY.
- Suivi d'ordre : bascule automatique sur /portfolio/fills quand le GET
  est indisponible ; tous les appels get_fills blindes (une panne de
  /fills ne peut plus interrompre un cycle).
- Garde anti-doublon de SESSION (SUBMIT_DEDUP_TTL_SECONDS, defaut 6 h) :
  un 201 sur un ticker verrouille le ticker, MEME si la verification
  echoue ensuite -- ce mode de defaillance ne peut plus vider le compte.
- Cooldown apres 503 exchange (EXCHANGE_503_COOLDOWN_SECONDS, defaut
  300 s) : plus de martelement pendant les maintenances du demo.
### Tests
- 135 tests (4 nouveaux reproduisant le scenario de prod exact), 0 echec.

## [12.4.0] — 2026-07-25
### Migration Create Order V2 (HTTP 410 deprecated_v1_order_endpoint)
- create_order migre vers POST /portfolio/events/orders, schema verifie
  contre l'OpenAPI officielle 3.20.0 (docs.kalshi.com) : side bid|ask sur
  le carnet YES uniquement (yes->bid ; achat NO a n cents -> ask a
  (100-n)/100 $), price en dollars fixed-point, count en chaine,
  time_in_force=good_till_canceled, self_trade_prevention=taker_at_cross.
- Reponse V2 SANS champ status : statut DERIVE de fill_count/
  remaining_count ; reponse normalisee vers le schema interne (compteurs
  entiers, average_fill_price dollars YES reconverti en cents sur NOTRE
  cote). accepted != filled preserve.
- Tout futur 410 "deprecated" sur un autre chemin leve une erreur de
  migration explicite ; audit : plus aucun POST /portfolio/orders dans le
  projet (teste par inspection de source), GET/DELETE ordres, fills,
  positions, markets, balance confirmes fonctionnels par les logs prod.
### Corrige — edge fantome +0.96 (bug revele par les memes logs)
- no_ask_dollars="1.0000" (=1,00$ = cote VIDE) etait parse 1 cent -> le
  moteur croyait acheter NO a 1c sur des cotes vides. Les champs *_dollars
  sont desormais STRICTEMENT interpretes en dollars ("1.0000"->None) ;
  1 legacy reste 1c. Le carnet KXBTCD reel du RAW ne produit plus de book.
### Remarques utilisateur
- Banner capital unifie : "plafond configure X$ | EFFECTIF = min(plafond,
  solde) = Y$" (plus d'affichage 500$ ambigu).
- Banner : conseil explicite EXECUTION_MODE=real_demo en demo ; chemins
  d'execution identiques (teste), seule la protection anti-mock change.
### Tests
- 131 tests (11 nouveaux), 0 echec.

## [12.3.0] — 2026-07-25
### Fournisseur de donnees BTC tolerant aux pannes (panne Binance en prod)
- Cause racine journalisee : klines Binance = point unique de defaillance ;
  son echec (code HTTP avale) annulait la volatilite => 100% des marches
  BTC rejetes no_model_probability.
- [DATA_PROVIDER] par fournisseur : http_status, elapsed_ms, error,
  accepted=true|false + raison (accepte=DEBUG, rejete=INFO).
- Chaine de secours klines : Binance -> Kraken OHLC -> Coinbase candles
  (ordre configurable KLINES_PROVIDER_ORDER) ; plus aucune dependance
  exclusive a Binance.
- Cache des dernieres bougies VALIDES (borne KLINES_STALE_MAX_S=600s) :
  une panne temporaire de tous les fournisseurs ne bloque plus les
  decisions ; en mode cache le MOMENTUM est neutralise (jamais rechauffe)
  et le score de qualite est penalise au prorata de l'age.
- Raisons distinctes et testees : aucune_donnee:klines|spot,
  donnees_insuffisantes:klines(n/11)|spot(n/2), volatilite_nulle.
### Tests
- 120 tests (10 nouveaux), 0 echec.

## [12.2.3] — 2026-07-25
### Audit Probability Engine (model_evaluated=0 avec supported>0)
- [MODEL_TRACE] par marche supporte : ticker, market_type, strategie,
  modele, executed=true|false, raison PRECISE (sous-raison complete :
  provider indisponible, donnees insuffisantes, contexte invalide, horizon,
  strike absent...). Plafond MODEL_LOG_MAX=10/cycle, MODEL_DEBUG=1 = tout.
- [MODEL-SUMMARY] en fin de cycle + model_rejections_detailed /
  model_executed dans le rapport : aucun marche supporte ne disparait
  silencieusement (invariant teste : supported == executed + rejets).
- [PROBABILITY_ENGINE] au demarrage : source de probabilite de chaque
  strategie + SONDE REELLE du fournisseur BTC (valid/reason/spot/sources/
  qualite) — revele immediatement dans Railway pourquoi un modele ne
  produit rien. PROBE_PROVIDERS_ON_START=0 pour desactiver.
- Verification EXECUTION_MODE : STANDARD ne restreint RIEN — seul le garde
  anti-mock et le banner lisent cette variable (teste par inspection de
  source + ordre place a l'identique en STANDARD).
### Tests
- 110 tests (5 nouveaux), 0 echec.

## [12.2.2] — 2026-07-25
### Corrige (audit no_liquidity : 80/80 rejetes en DEMO)
- Parseur de prix tolerant `price_to_cents` : cents entiers (48, "48.00"),
  dollars decimaux (0.48, "0.4800" -> 48c) et variantes de champs
  `*_dollars` (l'API v2 double desormais ses champs, cf. [RAW:balance]).
  Bug corrige : int(float("0.48"))=0 => carnet considere vide => 100% des
  marches rejetes no_liquidity. Meme correctif dans normalize_book.
- Le critere de liquidite n'a PAS ete assoupli : un carnet reellement vide
  (aucun prix, aucun volume/OI) reste rejete.
### Ajoute
- [LIQUIDITY_REJECT] par marche rejete : ticker, best_bid, best_ask,
  bid_size, ask_size, volume, open_interest, liquidity_score,
  rejected_reason=no_liquidity. Plafond LIQUIDITY_LOG_MAX=10/cycle
  (LIQUIDITY_DEBUG=1 pour tout) ; details COMPLETS dans le rapport scanner.
- [RAW:market_sample_<serie>] : un objet marche BRUT par serie journalise
  une fois par process (trou d'observabilite : les vrais noms de champs de
  l'API n'avaient jamais ete vus).
### Tests
- 105 tests (7 nouveaux), 0 echec.

## [12.2.1] — 2026-07-25
### Corrige (bug production Railway du 2026-07-25)
- `requirements.txt` : ajout de `cryptography>=42` (signature RSA-PSS des
  requetes Kalshi). Son absence causait "No module named 'cryptography'"
  avale en warning, puis HTTP 401 silencieux sur /portfolio a chaque cycle.
- Fail-fast : dependance `cryptography` absente => [FATAL] + exit 4 au
  demarrage, avec le remede dans le log.
- Cle RSA non chargee (absente/non-PEM) => tout appel /portfolio est
  refuse EXPLICITEMENT avant envoi (message actionnable), plus jamais de
  boucle de 401 muets ; les chemins publics ne sont pas bloques.
### Tests
- 98 tests (3 nouveaux), 0 echec.

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

## v12.7.0 — Institutional Dashboard Replacement
- Replaced the basic administrative layout with the Atlas Decision Engine terminal UI.
- Added responsive sidebar navigation, live system status, institutional KPI cards, equity and daily P&L charts.
- Added real-time opportunity table, positions/orders panel, risk gauges, cycle funnel, rejection diagnostics and activity stream.
- Preserved the existing `/api/state` contract and honest-data policy: no fabricated metrics.
- Kept the stdlib-only HTTP server and Railway `$PORT` compatibility.
