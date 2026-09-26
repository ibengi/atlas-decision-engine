# Corriger les défauts et rendre le moteur rentable

Réponse à une question : comment. Écrit le 2026-09-26, après la mesure qui
a fait échouer CLAIM A.

## La chose à dire d'abord

Les défauts sont réparables. Chacun a une cause identifiée et un correctif
de durée connue. **La rentabilité n'en découle pas.**

Un moteur sans défaut est un moteur qui mesure correctement. S'il mesure
correctement qu'il n'y a pas d'edge, il restera correctement à zéro ordre.
La rentabilité suppose qu'un edge existe et soit trouvable, et ça n'est pas
un livrable d'ingénierie : c'est un pari de recherche avec une vraie
probabilité de « non ».

Un plan qui promettrait la rentabilité serait le plan à ne pas croire. Ce
qui suit promet de savoir, vite, et pour pas cher.

## Phase 0 — quelques heures, données déjà sur le disque

Trois mesures. Aucune n'attend de nouvelles données, aucune ne coûte un
centime, et ensemble elles disent si le reste du plan vaut d'être lancé.

    python tools/model_ablation.py  $DATA_DIR/shadow_predictions.json
    python tools/shadow_census.py   $DATA_DIR/shadow_predictions.json
    python tools/shadow_pnl.py      $DATA_DIR/shadow_predictions.json \
                                    --brier-delta 0.0205

**L'ablation** rejoue le modèle sur les 13 519 observations réglées avec un
composant changé à la fois. Le suspect est nommé : `MOMENTUM_CAP = 0.5`
vaut `Phi(0.5) - Phi(0) = 19,1 points` de probabilité, et le moteur
journalise des edges nets de +19,2 % et +22,3 %. Un edge qui tombe sur la
constante d'écrêtage du modèle est un nombre que le modèle rapporte sur
lui-même, pas sur le marché. L'ablation tranche cette hypothèse au lieu de
la débattre.

**Le census stratifié** sépare les lignes par `data_quality`. Binance
répond 451 à la région US où tourne le service, donc les 13 519 prédictions
ont été servies par un fournisseur de repli ou par le cache périmé (jusqu'à
600 s), jamais par le primaire — et lequel n'est enregistré nulle part. Si
le taux réalisé suit le marché dans la bande haute et diverge dans la
basse, le défaut est dans les entrées. S'il diverge partout, il est dans le
modèle.

**Le rejeu P&L** demande si la sélection sauve ce que la prévision rate.

### Ce que chaque issue commande

| observation | cause | suite |
|---|---|---|
| une variante sans momentum bat le marché | le terme de momentum | corriger, revalider sur fenêtre neuve |
| divergence seulement en basse qualité | les entrées | Phase 1, puis remesurer |
| aucune variante ne bat le marché, toutes bandes | le modèle lui-même | Phase 2, et le pari devient long |
| le contrôle ne reproduit pas le store | lineage | à résoudre avant tout le reste |

L'ablation est une recherche, et une recherche biaise son propre gagnant.
Toute variante gagnante doit être **fixée à l'avance** et remesurée sur une
fenêtre jamais vue. C'est pourquoi l'outil ne rend que des verdicts
`DIAGNOSTIC_ONLY`.

## Phase 1 — une à deux semaines, ingénierie, résultat certain

Ces correctifs aboutissent. Ils ne créent pas d'edge ; ils font que ce
qu'on mesure ensuite veut dire quelque chose.

**D1 — intégrité des entrées.** Enregistrer le fournisseur et la fraîcheur
des klines dans `features`, par ligne : aujourd'hui `data_quality` est le
seul témoin et il est indirect. Réordonner `KLINES_PROVIDER_ORDER` pour que
le primaire ne soit pas un 451 garanti, ou sortir de la région US. Resserrer
`KLINES_STALE_MAX_S = 600` : dix minutes de volatilité périmée sur un
contrat de quinze minutes, c'est les deux tiers de sa vie. Ajouter un
contrôle inter-places sur `sigma_1m` et refuser la décision si deux sources
divergent.

**D3 — comptabilité du capital.** `capital = min(plafond, solde_broker)`
avec un solde à 0,06 $ fait lire une perte de 0,48 $ comme 827 % de
drawdown ; la même perte lisait 1001,9 % quand le solde était à 0,05 $. Le
dénominateur doit être le capital **alloué à la stratégie**, pas la
trésorerie courante. C'est un bug de mesure, et le corriger n'est pas
affaiblir la garde — c'est lui faire mesurer ce qu'elle prétend mesurer. La
perte de 0,48 $ reste au livre : remettre le journal à zéro pour débloquer
le LIVE serait un échec, pas un correctif.

**D4 — séparer le compte.** Le verrou de réconciliation s'est armé trois
fois le 26 septembre sur des positions fractionnaires manuelles
(`position_fp='43.10'`, ticker LaLiga, hors de toute stratégie Atlas). La
protection a fonctionné à chaque fois. Le défaut n'est pas là : Atlas ne
sait pas distinguer un ordre manuel du sien, donc toute activité tierce
l'arrête, et en LIVE ça arriverait à un instant arbitraire. Le correctif le
moins cher est un sous-compte Kalshi dédié où personne ne trade à la main :
les positions broker deviennent les positions Atlas par construction.

**D5 — lineage.** `model_validation.json` déclare `btc15m-baseline-0.1` au
commit `acb9c01b` ; la production estampille `btc15m-v1.0-ref`. Régénérer
l'artefact contre le modèle réellement déployé, en y inscrivant les nombres
mesurés, `approved: false`. Quelques heures.

## Phase 2 — calibration, semaines, résultat incertain

Le modèle est analytique : mouvement brownien géométrique sans drift, `d =
ln(spot/strike) / (sigma_1m * sqrt(T))`. L'échelle en racine du temps
suppose des rendements i.i.d. ; à l'échelle de la minute le bruit de
microstructure rend cette hypothèse fausse dans un sens qui dépend du
régime. Le correctif empirique n'est pas de mieux théoriser, c'est de
**calibrer sur les 13 519 observations réglées** — régression isotone ou
Platt, probabilité modèle vers fréquence réalisée. C'est ce que
`model_calibration.json` devait être, et il est absent de `/data/state5`.

À dire franchement : la calibration **retire la surconfiance, elle ne crée
pas d'edge**. Une fois calibré, si la probabilité du modèle rejoint celle du
marché, il n'y a rien à prendre et le moteur ne tradera pas. C'est l'issue
la plus probable, et c'est une issue correcte.

## Phase 3 — trois à quatre semaines d'attente incompressible

Les 13 519 observations actuelles ont été produites avec les entrées non
corrigées et ont servi à choisir une variante. Elles ne peuvent pas valider
le correctif : ce serait se noter sur sa propre copie. Il faut une fenêtre
**neuve**, le correctif figé à l'avance, et la même mesure qu'au départ.

Rien ne raccourcit cette attente. C'est le prix d'une réponse qui vaut
quelque chose.

## Phase 4 — canary, conditionnel

À n'ouvrir que si tout est vrai en même temps : Brier OOS battant l'ask sur
la fenêtre neuve, P&L de rejeu positif après traversée du spread, ≥ 300
règlements sur un nombre de dates indépendantes que l'opérateur doit encore
fixer (T7-I §9 le note « REQUIRES OPERATOR APPROVAL »), capital réel avec
dénominateur correct, compte séparé, lineage cohérent. Puis un contrat, une
série, une perte plafonnée d'avance.

## Calendrier et probabilité, sans enjolivure

* Phase 0 : **aujourd'hui**, trois commandes.
* Phase 1 : 1 à 2 semaines.
* Phases 2 et 3 en recouvrement : 4 à 6 semaines.
* Canary honnête au plus tôt : **6 à 8 semaines**, conditionnel.

Probabilité que les mesures de Phase 3 reviennent positives : à mon
estimation **20 à 35 %**. Le marché visé est liquide, l'écart entre taux
réalisé (0,4967) et implicite (0,5077) est de 1,1 point, et le spread le
mange. Prédire mieux qu'un carnet liquide sur BTC à quinze minutes est
difficile, et ce modèle est une baseline analytique.

Si elles reviennent négatives, la réponse n'est pas « encore six semaines ».
C'est que l'edge n'est pas dans la prévision de direction, et qu'il faut
soit changer de terrain — microstructure, cotations périmées, séries moins
suivies — soit arrêter. Les deux sont des décisions du propriétaire.

## Ce qui a déjà de la valeur

L'appareil de mesure. Un moteur connecté à la production réelle, qui s'est
mesuré lui-même et a refusé d'engager du capital sur un modèle sans edge —
avant d'avoir perdu un dollar. Le modèle est le composant remplaçable ;
l'appareil qui l'a réfuté est ce qui rend la suite possible.
