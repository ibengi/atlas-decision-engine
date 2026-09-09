# Atlas Decision Engine

Moteur de décision autonome pour marchés de prédiction (Kalshi) :
scan ciblé de l'univers, classification déterministe des marchés, modèles de
probabilité, portes d'edge/EV nettes de frais, gestion du risque en % du
capital effectif, et exécution démo avec confirmation des fills par l'API.

> ⚠️ **Avertissement** — Ce logiciel est fourni à des fins éducatives et de
> recherche. Le trading comporte un risque de perte totale. Rien ici ne
> constitue un conseil financier. Le mode LIVE est verrouillé par défaut
> (`NO_LIVE_PROMOTION=1`) et exige des confirmations explicites multiples.

## AI Alpha Gateway (shadow only)

An optional subsystem that asks Grok, Gemini, OpenAI and an Atlas
quantitative model for independent probability estimates, combines them
through a Meta Alpha Engine, and measures whether the ensemble produces
cost-adjusted edge against Kalshi prices.

**It cannot trade.** It holds no broker client, no order manager and no risk
manager; no `alpha_*` module imports the execution path and no execution
module imports it, both enforced by AST inspection in
`tests/test_alpha_safety_boundary.py`. None of its eight terminal states
means TRADE. It is off by default (`ALPHA_GATEWAY_ENABLED`, strict
fail-closed) because enabling it starts paid third-party API calls.

Phase 2 runs it automatically: the scanner's read-only observer emits
candidates to a spool, and a **separate** Alpha Shadow Service consumes them.
That service refuses to start if a broker credential is visible in its
environment.

    # engine process (feed off by default)
    RESEARCH_FEED_ENABLED=true python kalshi_alpha_bot.py --loop --live-read-only

    # alpha service process: AI keys only, no broker credential
    python tools/alpha_service_run.py health
    python tools/alpha_service_run.py run

    # manual / one-off analysis and reporting
    python tools/alpha_shadow_run.py analyze --input candidates.json
    python tools/alpha_shadow_run.py metrics

Phase 3 points the adapters at the real vendor surfaces (`grok-4.6` and
`gpt-5.6-luna` on the Responses API, `gemini-3.7-flash` on `generateContent`)
and prices them in `alpha_pricing.json`, where every rate carries its source
and its validity window. Before an automatic real-provider session may
start, each provider must pass a one-call smoke test:

    # requires XAI_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY in the
    # environment; makes exactly ONE bounded call per provider against a
    # fixture market that does not exist, charged to the daily budget
    python tools/alpha_smoke_test.py
    python tools/alpha_smoke_test.py --provider grok --json

A provider that fails is EXCLUDED. Nothing falls back to another model and
no missing answer becomes a probability.

On Railway the engine and Alpha are **two services from this one
repository**, with different start commands and different authority: the
engine holds the Kalshi credentials, Alpha holds only the AI keys and
refuses to start (exit 78,
`ALPHA_STARTUP_REFUSED_BROKER_CREDENTIALS`) if it can see a broker
credential or a write gate. Because a Railway volume is mounted into exactly
one service, the research spool crosses the boundary over the engine's
read-only research API (`ALPHA_FEED_TRANSPORT=http`) rather than a shared
directory — an unreachable feed is reported as an error, never as an empty
one. Runbook:
[`docs/ops/alpha-railway-deployment.md`](docs/ops/alpha-railway-deployment.md).

Design: [`docs/design/alpha-gateway.md`](docs/design/alpha-gateway.md).
The shipped endpoints, model ids and rates are **operator-supplied and
unverified by this repository** — check them against each vendor's current
API reference and pricing page before enabling anything. A model with no
applicable rate is not called at all, because an uncosted call would make
every spending cap unenforceable.

## Architecture

```
atlas-decision-engine/
├── run.py                  # point d'entrée (CLI)
├── run_tests.py            # suite de tests → test_report.json
├── src/
│   ├── engine/             # moteur d'exécution, pipeline d'opportunités,
│   │                       #   ordres, positions, risque, client API
│   ├── ai/                 # modèles de probabilité (BTC), calibration,
│   │                       #   contexte marché, shadow store, gatekeeper LIVE
│   ├── strategies/         # registre canonique market_type → stratégie
│   ├── scanner/            # scan ciblé, cache univers, classification,
│   │                       #   ranking de tradabilité
│   ├── dashboard/          # statut CLI (UI web : voir docs/roadmap.md)
│   └── utils/              # bootstrap de chemins
├── tests/                  # 68 tests, hors-ligne, déterministes
├── examples/               # backtest chronologique BTC 15m, env.example
└── docs/                   # architecture, IA, risque, déploiement, API
```

## Démarrage rapide (mode DÉMO uniquement)

```bash
pip install -r requirements.txt
cp examples/env.example .env        # renseigner les clés DEMO Kalshi
export $(grep -v '^#' .env | xargs)  # ou votre gestionnaire d'env

python run.py --demo --scan-only     # vérifier le scanner
python run.py --demo --loop --shadow # observer sans passer d'ordres
python run_tests.py                  # doit afficher: 68 tests, OK
python src/dashboard/status.py "$DATA_DIR"
```

Le passage d'ordres démo (sans `--shadow`) nécessite des clés API **démo**
valides. Les clés de production ne sont jamais utilisées en mode démo.

## Principes de conception

1. **Fail-fast** : registre de stratégies validé au démarrage ; un registre
   vide ou incomplet arrête le moteur (exit 2).
2. **Déterminisme** : classification par préfixe de série du ticker, jamais
   par sous-chaînes de titres.
3. **Prix exécutables** : entrée au *ask* (achat) / *bid* (vente), jamais
   `last_price` ; edge et EV **nets** de frais, slippage et tampon
   d'incertitude.
4. **Capital effectif** : toutes les limites de risque sont recalculées sur
   `min(solde broker, plafond configuré)` à chaque cycle.
5. **Vérité API** : un trade n'existe qu'après confirmation du fill par
   l'endpoint fills ; le PnL n'est réalisé que sur le `result` officiel.
6. **LIVE sous clé** : tests verts < 7 j + validation modèle < 30 j +
   levée explicite de `NO_LIVE_PROMOTION` + triple confirmation d'env.

## Tests

```bash
python run_tests.py
# Ran 68 tests ... OK  → écrit test_report.json (consommé par le gatekeeper)
```

Les tests exercent le pipeline réel avec un client API factice injecté :
aucun réseau, aucun aléatoire.

## Documentation

- [docs/architecture.md](docs/architecture.md) — flux de données et modules
- [docs/ai_engine.md](docs/ai_engine.md) — modèles, calibration, shadow
- [docs/risk_engine.md](docs/risk_engine.md) — limites et invariants
- [docs/deployment.md](docs/deployment.md) — Railway / conteneur
- [docs/api.md](docs/api.md) — surface API Kalshi utilisée
- [docs/roadmap.md](docs/roadmap.md) — travaux prévus et limites connues
- [docs/audit_2026-07.md](docs/audit_2026-07.md) — audit des causes racines

## Statut et limites connues

- Seules les stratégies **BTC** (daily et 15 min) produisent une probabilité
  modèle. Les stratégies sports/élections sont routées mais refusent de
  trader tant qu'un fournisseur de probabilités calibrées n'est pas injecté
  (voir `ProviderBackedStrategy`). C'est un choix délibéré : pas de
  probabilité inventée.
- L'heuristique qualité→confiance n'est pas encore calibrée sur un volume
  suffisant de prédictions shadow réglées.
- Voir la section « points non validés » de
  [docs/audit_2026-07.md](docs/audit_2026-07.md).

## Licence

Distribué sous licence MIT — voir [LICENSE](LICENSE).

## Contribuer

Voir [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) et
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
