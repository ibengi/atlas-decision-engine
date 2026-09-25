# Atlas V2 — RESEARCH / READ_ONLY

This branch is the isolated V2 rebuild authority. The active image contains only
the [V2 package](v2/README.md), a public GET-only data collector and offline
research/simulation primitives. No predictive model is selected or approved;
CAPITAL remains OFF and no broker mutation capability is shipped.

See [authority and salvage decisions](docs/ATLAS_V2_AUTHORITY.md). V1 is frozen at
`atlas-v1-final-readonly` / `bd810b4f3177108032f85e21a55729b6eb4b5a53`.
The documentation and root modules below are retained as legacy history; their
commands are not V2 entrypoints. Do not reactivate the failed V1 alpha.

---

# Legacy Atlas Decision Engine reference

Moteur de décision autonome pour marchés de prédiction (Kalshi) :
scan ciblé de l'univers, classification déterministe des marchés, modèles de
probabilité, portes d'edge/EV nettes de frais, gestion du risque en % du
capital effectif, et exécution démo avec confirmation des fills par l'API.

> ⚠️ **Avertissement** — Ce logiciel est fourni à des fins éducatives et de
> recherche. Le trading comporte un risque de perte totale. Rien ici ne
> constitue un conseil financier. Le mode LIVE est verrouillé par défaut
> (`NO_LIVE_PROMOTION=1`) et exige des confirmations explicites multiples.

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
