# Déploiement

## Railway (ou tout conteneur)
- **1 réplique uniquement** : pas de verrou distribué multi-instances.
- Volume persistant monté sur `DATA_DIR` (états JSON, caches, JSONL).
- Commande : `python run.py --demo --loop` (ajouter `--shadow` pour la
  phase d'observation recommandée de 24 h minimum).
- Variables : voir `examples/env.example`. Clés **démo** obligatoires en
  mode démo ; les clés prod ne sont jamais lues en démo.

## Passage LIVE (verrouillé par conception)
Toutes ces conditions simultanément :
1. `python run_tests.py` vert, `test_report.json` < 7 jours ;
2. `model_validation.json` approuvé < 30 jours (validation shadow) ;
3. `NO_LIVE_PROMOTION` explicitement levé (défaut = 1) ;
4. `KALSHI_ENV_CONFIRM=LIVE`, `LIVE_TRADING_CONFIRMED=YES`, `LIVE_TRADING=1`.

## Observabilité
- 1 ligne `[CYCLE-SUMMARY]` JSON par cycle (funnel complet + cycle_id) ;
- `decisions.jsonl` rotatif (5 Mo × 3) avec decision_id ;
- échantillons `[RAW:*]` de réponses API au premier appel de chaque type —
  à vérifier après le premier déploiement (champs frais, avg_price).
