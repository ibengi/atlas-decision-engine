# ATLAS V2 — contre-audit indépendant du candidat

Conduit le 2026-09-26. Je ne suis pas l'auteur du modèle. Rien n'a été
amélioré, réglé, ni proposé comme seuil favorable. Aucun déploiement,
aucun merge, aucun capital, aucune écriture broker.

## Verdict

    CANDIDATE_REJECTED: aucun candidat entraîné n'existe — CHALLENGER_REGISTRY
    déclare status=PREREGISTERED_NOT_TRAINED, les deux challengers
    MR-STRUCTURAL-1 et MR-REGIME-1 sont NOT_TESTABLE, la fenêtre TRAIN
    s'ouvre le 2026-09-27T00:00:00Z et fit_at est le 2026-10-14T01:00:00Z ;
    aucun lock n'est persisté et il n'existe aucune métrique décisive à
    recalculer.

Un audit qui doit recalculer indépendamment les métriques décisives ne peut
pas rendre SURVIVES quand aucune métrique n'existe. Ce rejet ne constate
aucune tromperie : les artefacts déclarent eux-mêmes cet état.

## Ce qui a été examiné

Branche `atlas-v2/phase2-alpha-lab`, HEAD `f322cb6` (2026-09-26 11:22 -0700),
la plus avancée des quatre branches `atlas-v2/*`. Fetch en profondeur 100,
greffe vérifiée.

| point | constat |
|---|---|
| provenance du jeu de données | **non vérifiable** — `FULL_STORE_ABLATION_REPORT.md` est `BLOCKED_BY_OPERATOR_DATA_EXPORT`, aucun export natif fourni |
| bornes chronologiques | **conflit**, voir ci-dessous |
| fuite d'entraînement | **non évaluable** — aucun entraînement n'a eu lieu |
| fuite de cible / de features | non évaluable, même raison |
| lineage modèle/release | `v1_use: consumed diagnostic evidence only` — correct, les données V1 sont déclarées brûlées |
| horodatage de lock du candidat | **aucun lock** — le registre le conditionne à la survie TRAIN/CALIBRATION/VALIDATION, non commencée |
| procédure de calibration | spécifiée (Platt, grille fixe, fenêtre séparée), **non exécutée** |
| discipline de recherche d'hyperparamètres | grille fixe, `report every tried configuration`, sélection sur TRAIN seul — discipline correcte sur le papier |
| multiplicité | **incohérente**, voir ci-dessous |
| baseline marché appariée | spécifiée correctement : midpoint apparié sur la même observation, ask reporté en supplément |
| complétude frais/slippage | politique explicite, `fee: no default zero`, borne de slippage positive — correct sur le papier |
| calcul de PnL | `allocation explicite, no broker-cash denominator` — corrige le défaut de dénominateur identifié en V1 |
| dépendance événement/jour | blocs jours UTC, `ticker` en cluster de repli, et le document le signale lui-même comme pouvant sous-estimer la dépendance |
| autorité de règlement | reçus de règlement natifs exigés, pas d'auto-attestation |
| reproductibilité | 160 tests OK, mutations tuées ; **prouve le logiciel, pas un résultat** |
| invariants de repricing d'exécution | entrée à l'ask du côté sélectionné, une position par marché, mouvement adverse décision→refresh compté |

## Le défaut de fond, au-delà de « pas encore entraîné »

Deux protocoles coexistent au même commit, sur **le même marché**
(KXBTC15M) et **le même magasin**, sans qu'aucun ne mentionne l'autre :

* `v2/TRAINING_PROTOCOL.json` — `PHASE2-20260926-1`, 5 familles
* `v2/atlas_v2/CHALLENGER_REGISTRY.json` — `MR-20260926-1`, 2 familles

Leurs fenêtres se contredisent sur **10 jours calendaires** :

    2026-10-04 -> 10-07   CALIBRATION (PHASE2)   et   TRAIN (MR)
    2026-10-07 -> 10-11   VALIDATION  (PHASE2)   et   TRAIN (MR)
    2026-10-11 -> 10-14   VALIDATION  (PHASE2)   et   CALIBRATION (MR)

Les mêmes lignes seraient des lignes d'ajustement pour un protocole et des
lignes retenues pour l'autre. Quel que soit celui évalué en second, sa
tranche « hors échantillon » aura servi à ajuster l'autre. C'est une fuite
par construction, et elle n'est écrite nulle part parce qu'aucun des deux
documents ne connaît l'existence du second.

Divergences associées :

    OOS            PHASE2 = 7 jours / 210 événements    MR = 28 jours / 840
    multiplicité   PHASE2 = « report all five families » (max 5 challengers)
                   MR     = Bonferroni sur 2 familles actives
    grille pente   PHASE2 a=[0.5,0.75,1,1.25,1.5]       MR=[0.75,1.0,1.25]
    grille interc. PHASE2 c=[-0.2,-0.1,0,0.1,0.2]        MR=[-0.25,0,0.25]

`MODEL_REDESIGN_SPEC.md` déclare `CHALLENGER_REGISTRY.json` autoritaire pour
MR, mais rien ne déclare `TRAINING_PROTOCOL.json` supersédé, et les deux
sont présents au HEAD. Si les deux expériences sont réellement distinctes,
la multiplicité réelle est de 7 familles, corrigée nulle part comme telle.

**Ce point doit être résolu avant le 2026-09-27T00:00:00Z.** Passé cette
date les jours commencent à être consommés, et un conflit de rôle ne se
répare pas après coup : il faut re-collecter.

## Ce que le candidat fait bien, et qu'il faut noter

Le design corrige sans qu'on le lui demande deux défauts établis en V1 :
la formule de base est `Phi(log(spot/strike)/(sigma_1m*sqrt(T)))` avec
**momentum retiré**, ce qui correspond exactement au diagnostic de Phase 0 ;
et le dénominateur de drawdown devient une allocation explicite au lieu de
la trésorerie broker. Ce n'est pas une autorisation, c'est une
reconnaissance que l'artefact n'est pas naïf.

## Une correction qui me vise

`FULL_STORE_ABLATION_REPORT.md` reproche au commit `6041679` — le mien —
de faire défaut à zéro les coûts manquants et de séparer les lignes de
trade des lignes de prévision. **Le reproche est fondé.**
`tools/shadow_pnl.py:121` fait bien `abs(float(v)) if v is not None else 0.0`.
J'avais signalé que le contrôle en devenait optimiste, mais compter un coût
absent comme nul reste faux : une couverture de coûts incomplète rend le
PnL **inconnu**, pas nul. Le correctif est dû ; je ne l'applique pas dans ce
tour, qui est un audit et non une amélioration.

## Rappel

Un audit survécu n'aurait pas été une autorisation de capital. Celui-ci
n'est pas survécu.
