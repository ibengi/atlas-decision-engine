# Phase 0 — résultats

Exécutée le 2026-09-26. Aucun ordre, aucune écriture broker, aucun capital,
aucune limite de risque touchée, aucun ledger remis à zéro, aucun
`approved: true`, aucun déploiement, aucun merge, aucune donnée historique
modifiée.

## L'échantillon, et pourquoi il faut le dire avant les résultats

Le store de production `/data/state5/shadow_predictions.json` n'est pas
accessible depuis ce conteneur. Phase 0 a tourné sur un **extrait réel**
conservé dans le scratchpad de session :

    dataset sha256  b38de14681f21580052ff39b5751491064a6f516d90c3a3e1f8adbffaf05ac87
    182 enregistrements, 147 réglés
    2026-09-01T06:02:39Z  ->  2026-09-02T03:49:13Z
    figé le 2026-09-02

Sa provenance est établie : son premier `ts` est exactement celui que le
census de production rapporte comme début de fenêtre utilisable. C'est le
**préfixe** authentique du store.

C'est **1,35 % des 13 526 enregistrements**, les 22 premières heures, et —
le point décisif — **une seule date de règlement**. L'échantillon compte
147 lignes mais environ **une observation indépendante**. Rien ici n'est un
verdict out-of-sample ; tout ce qui suit est un diagnostic à confirmer sur
le store complet.

    sorties     ablation.json  16c4066a63ec3ab67d70c8a24fe37160519b0198d75f4502f70b9893708495a3
                census.json    fd7ed1adcd333ce85d2622117c8eff215f1a377f64a0927f55baa2cc3c903b00
                pnl.json       2fe54da3490d15f5068185c0c93bc4e2273daa2fcb3f4ac5178e9849e1a4428e

## Objectif 1 — le contrôle reproduit-il le modèle déployé ? OUI

    147 lignes vérifiées, 0 divergente, écart absolu maximal = 0.0

Le modèle de l'arbre reproduit **au bit près** la probabilité que le store
a enregistrée, sur chaque ligne. `btc15m-v1.0-ref` sur 147/147.

**LINEAGE_BLOCKER est écarté** pour le code. La divergence reste entière au
niveau de l'artefact : `model_validation.json` déclare
`btc15m-baseline-0.1` alors que le code déployé est `btc15m-v1.0-ref`. Le
défaut est dans l'artefact d'approbation, pas dans le modèle.

## Objectif 2 — l'effet du momentum : c'est là qu'est le défaut

Sur la tranche TEST (30 lignes), écart de Brier au marché :

| variante | Brier | delta vs marché |
|---|---|---|
| `as_recorded` (cap 0,50) | 0,257714 | **+0,019234** |
| `no_momentum` | 0,238946 | **+0,000466** |
| `momentum_cap_0.25` | 0,246236 | +0,007756 |
| `momentum_cap_0.10` | 0,241554 | +0,003074 |
| `sigma_x1.5` | 0,245587 | +0,007107 |
| `sigma_x0.67` | 0,273958 | +0,035478 |

Retirer le momentum efface **97,6 %** de l'écart au marché.

Dose-réponse sur les 147 lignes, monotone sur six valeurs :

    cap=0.00  brier=0.190864     cap=0.25  brier=0.197944
    cap=0.05  brier=0.191463     cap=0.50  brier=0.210030
    cap=0.10  brier=0.192730     cap=1.00  brier=0.231493

Plus le plafond est grand, pire le modèle. Du bruit ne produit pas une
monotonie sur six points, dans le même sens, sur deux tranches.

Test apparié, n=147, même ligne, même issue, un seul terme changé :

    Brier(avec) - Brier(sans) = +0,019166
    erreur-type                =  0,009973
    t                          = +1,92

`t = 1,92` n'est **pas** significatif au seuil usuel, et avec une seule
date de règlement l'erreur-type réelle est plus grande que celle-ci. Ce
n'est pas la preuve ; la monotonie et le mécanisme le sont.

### Le mécanisme

    mu = clip( (ret_5m/5) * T / (sigma_1m * sqrt(T)), +/- 0.5 )

    |mu| sature le plafond sur          106/147 lignes  (72,1 %)
    dégât Brier moyen si saturé          +0,023738
    dégât Brier moyen si non saturé      +0,007344
    part du dégât portée par les saturées   89,3 %
    |raw| médian avant écrêtage          0,89      |raw| max  4,9

Le terme de momentum n'est pas un ajustement fin : il est **collé à sa
borne trois fois sur quatre**, et le brut vaut couramment 2 à 10 fois le
plafond. Le modèle sort donc, la plupart du temps, `Phi(d ± 0.5)` — un
décalage de ±19,1 points de probabilité produit par un terme qui a
explosé sa propre borne.

C'est exactement la magnitude des edges nets que le moteur journalise :
**+19,2 %** et **+22,3 %**.

## Objectif 3 — entrées ou modèle ? Les entrées sont hors de cause ICI

    data_quality : 147/147 dans la bande 90-100
    lignes sans data_quality enregistrée : 0
    attrition : 35 lignes, toutes « unsettled », aucune perdue faute de quote

Sur cette fenêtre les entrées étaient bonnes. **DATA_QUALITY_IS_PRIMARY_-
DEFECT est écarté pour le 1er septembre, et pour lui seul** : les 98,65 %
restants ne sont pas couverts, et le 451 de Binance observé le 26 septembre
n'est ni confirmé ni infirmé pour cette date.

## Objectif 4 — le P&L : INDÉTERMINÉ, et il faut le dire ainsi

    décisions tradées dans tout l'extrait : 8
    tranche TEST : 3 contrats, net -0,95 $
    verdict : INDETERMINATE_SAMPLE_TOO_SMALL

Trois contrats ne décident rien, et l'outil refuse de conclure — c'est son
travail. **Ni `UNPROFITABLE` ni `SELECTION_WITHOUT_FORECAST_EDGE` ne
peuvent être retournés honnêtement.** Deuxième réserve : seules 10 lignes
sur 182 portent un `estimated_fee`, donc le rejeu facture un coût nul sur
presque tout — le contrôle « trader tout en YES » à +0,199 $/contrat est
optimiste et ne doit pas être lu comme un résultat.

## Le piège de cet extrait, à ne pas se laisser vendre

Sur les 147 lignes complètes, le modèle **bat** le marché :

    modèle (tel que déployé)  brier = 0,210030
    marché (ask)              brier = 0,261702      delta = -0,051672

Ce nombre est flatteur et il ne vaut rien. Le census en donne la raison :

    taux réalisé YES     0,367347
    implicite marché     0,481905      écart = -0,114558

Le 1er septembre, le marché a sur-évalué YES de 11,5 points et le modèle
penchait NO. Il a gagné parce qu'il était biaisé dans le sens où la journée
est allée — sur **une** journée. Ce n'est pas un edge, et la tranche TEST
comme la production disent l'inverse.

## Conclusion

**Diagnostic principal : `MOMENTUM_IS_PRIMARY_DEFECT`.**

Écartés par la mesure : `LINEAGE_BLOCKER` (147/147, écart 0,0),
`DATA_QUALITY_IS_PRIMARY_DEFECT` (147/147 en bande haute, sur cette date),
`MODEL_REBUILD_REQUIRED` — le défaut est localisé dans un terme et une
constante, pas réparti dans la formule.

Sur le P&L, aucun des libellés proposés ne s'applique : la réponse est
INDÉTERMINÉ, faute de décisions.

### Une reconstruction complète du modèle est-elle nécessaire ? NON

Le défaut est un terme, et sa constante. `MOMENTUM_CAP = 0.5` est écrêté
72 % du temps et porte 89 % du dégât. `use_momentum=False` ou un plafond de
0,05 est un changement d'une ligne.

### Mais cela ne rend pas le moteur rentable

    écart production au marché        +0,020500
    effet du momentum mesuré          -0,019166
    écart résiduel estimé             +0,001334

Retirer le momentum amène le modèle **à parité** avec le marché, pas
au-dessus. Un modèle qui égale le carnet n'a rien à prendre une fois le
spread et les frais payés. Le défaut est identifié et réparable en une
ligne ; la rentabilité, elle, reste à trouver ailleurs.

C'est une bonne nouvelle et une mauvaise, et les deux sont vraies.

## Ce qu'il faut corriger en premier

1. **Le momentum**, parce que c'est une ligne et que ça supprime les faux
   edges à +22 % qui polluent toute décision en aval.
2. **Relancer Phase 0 sur le store complet** — les 13 526 lignes, 25 dates
   de règlement. Tout ci-dessus repose sur une seule journée.
3. **L'artefact de lineage**, quelques heures, indépendant du reste.

Le verdict d'audit ne bouge pas : **STOP_AND_PARK_ATLAS**.
