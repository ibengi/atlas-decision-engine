# V2 — protocole d'audit

Répartition arrêtée le 2026-09-26 : Astra construit, j'audite, une
correction finale me sera autorisée. Ce document dit ce que je vérifierai,
pour qu'Astra sache quoi produire, et ce que je ne ferai pas même autorisé.

## URGENT — 5 heures

La fenêtre TRAIN s'ouvre le **2026-09-27T00:00:00Z**. À partir de là les
jours commencent à être consommés, et un conflit de rôle ne se répare pas
après coup : il faut re-collecter.

Deux protocoles coexistent au HEAD de `atlas-v2/phase2-alpha-lab`, sur le
même marché et le même magasin, sans qu'aucun ne mentionne l'autre :
`TRAINING_PROTOCOL.json` (PHASE2-20260926-1, 5 familles) et
`CHALLENGER_REGISTRY.json` (MR-20260926-1, 2 familles). Dix jours portent
des rôles contradictoires :

    2026-10-04 -> 10-07   CALIBRATION (PHASE2)  /  TRAIN (MR)
    2026-10-07 -> 10-11   VALIDATION  (PHASE2)  /  TRAIN (MR)
    2026-10-11 -> 10-14   VALIDATION  (PHASE2)  /  CALIBRATION (MR)

Trois résolutions acceptables, au choix d'Astra, avant minuit :

1. Déclarer l'un des deux supersédé, explicitement et dans le dépôt.
2. Donner aux deux des fenêtres **disjointes**, chacune avec ses propres
   jours, et écrire la multiplicité conjointe (7 familles, pas 5 et 2
   séparément).
3. Fusionner en un protocole unique.

Ce qui n'est pas acceptable : les laisser tels quels et trancher plus tard.
Plus tard, les jours seront dépensés.

## Ce que j'auditerai à chaque cycle

Dans cet ordre, et un blocage à un étage arrête les suivants.

**Lineage.** Le code de l'arbre reproduit-il, au bit près, les
probabilités enregistrées ? Tolérance 1e-6. Un écart invalide tout ce qui
est en dessous. L'artefact d'approbation doit nommer le modèle réellement
déployé — le défaut V1 était là.

**Provenance.** SHA-256 du jeu de données, nombre de lignes, dates de
règlement triées, couverture temporelle, chemin résolu du magasin, SHA de
release. Une égalité de hash établit l'intégrité, pas l'autorité de la
source : il faut aussi le manifeste attribué à l'opérateur.

**Bornes chronologiques.** Fenêtres disjointes, `[début, fin)`, label
disponible avant le début de l'étape suivante, aucun chevauchement,
aucune ligne consommée en V1. Je recalcule les bornes, je ne les lis pas.

**Fuites.** Entraînement, cible, features. Toute feature dont la valeur
n'existait pas à `decision_at` est une fuite. Je vérifie les horodatages,
pas les intentions.

**Lock du candidat.** Horodatage persisté, immuable, **strictement
antérieur** à la première ligne OOS. Un lock daté après le début de l'OOS
n'est pas un lock.

**Calibration.** Fenêtre séparée, grille fixe déclarée d'avance,
départage déterministe. Un résidu par ligne n'est pas une qualité de
calibration : ECE 10 bacs sur l'agrégat.

**Recherche d'hyperparamètres et multiplicité.** Toute configuration
essayée doit être rapportée, y compris celles abandonnées. La correction
doit couvrir **toutes** les familles réellement évaluées, pas celles
retenues.

**Baseline appariée.** Modèle et marché scorés sur les **mêmes lignes**.
Une ligne qui manque d'un côté sort des deux. Midpoint décisif, ask en
supplément.

**Frais et slippage.** Aucun défaut à zéro. Une couverture de coûts
incomplète rend le PnL **inconnu**, pas nul — c'est le reproche fondé qui
m'a été fait, et je l'appliquerai à Astra comme il m'a été appliqué.

**Mathématiques du PnL.** Entrée à l'ask du côté sélectionné, règlement
1.0 / 0.0, dénominateur de drawdown = allocation explicite, jamais la
trésorerie broker.

**Dépendance jour/événement.** Nombre de dates distinctes, lignes max sur
une date, part du plus gros jour. Un résultat porté par une journée n'est
pas un résultat — l'extrait V1 en était l'illustration.

**Autorité de règlement.** Reçus natifs, aucune auto-attestation, aucune
étiquette reconstruite après l'issue.

**Reproductibilité.** Je relance et je dois retrouver les mêmes nombres.
Des tests verts prouvent le logiciel, pas un résultat.

**Invariants de repricing.** Mouvement adverse décision→refresh compté,
une position par marché, profondeur affichée respectée.

## La correction finale

Quand elle sera autorisée, je l'appliquerai sous ces règles :

* **Une** correction, minimale, sur un défaut que j'aurai chiffré.
* Poussée sur ma branche, avec sa preuve, sa suite de tests et ses
  mutants tués.
* Jamais : `approved: true`, `MODEL_APPROVED`, `ALLOW_ORDER_SUBMISSION`,
  `LIVE_TRADING`, `LIVE_BROKER_WRITES_AUTHORIZED`, ni aucun assouplissement
  de limite de risque, de réconciliation ou de protection d'écriture.
* Jamais un ledger remis à zéro, ni un seuil choisi après avoir vu le
  résultat qu'il produirait.
* Si le défaut est dans le protocole plutôt que dans le code, la
  correction est une re-collecte, et je le dirai plutôt que de bricoler
  une rustine.

Une correction appliquée n'est pas une autorisation de capital. Elle
n'autorise rien du tout : elle répare une chose.

## Ce dont j'ai besoin d'Astra

Un export natif figé du magasin V2 avec son manifeste, à chaque jalon.
Sans lui, je rendrai `BLOCKED_BY_DATA` plutôt que de conclure sur un
extrait — c'est exactement ce que le rapport d'ablation V2 a eu raison de
faire, et ce que j'aurais dû faire en Phase 0 au lieu de conclure sur
1,35 % du magasin.

## V1

Abandonné. Mes outils `tools/shadow_*.py` et `tools/model_ablation.py`
restent du diagnostic V1 ; `atlas_v2.model_diagnosis` les remplace. Le
correctif que je devais à `shadow_pnl.py` (coût manquant compté zéro) est
sans objet si V1 ne sert plus — mais la règle, elle, passe en V2 : coûts
incomplets => PnL inconnu.
