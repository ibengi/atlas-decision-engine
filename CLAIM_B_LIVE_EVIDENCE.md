# CLAIM B observé en production — la garde de drawdown s'est levée seule

Relevé le 2026-09-26 entre 20:54Z et 21:05Z, déploiement
`f15db78b-8061-4403-971c-045afec8418a`, SHA épinglé `bd810b4f`. Aucun
déploiement, aucune correction, aucune écriture.

## La chaîne, dans l'ordre

    18:50:18Z  verrou armé   KXUEFANLTOTAL-...-3    position_fp='-102.71'
    19:37:25Z  puis          KXUEFANLGAME-...-ESP   position_fp='249.03'
    20:54:45Z  [CAPITAL] solde=249.11$          (cycle 345, verrou encore armé)
    20:55:48Z  [RECONCILE_VERIFY] retablissement -- verrou leve
               [RECONCILE_VERIFY] MATCH (tickers broker=0 local=0)
    20:55:48Z  cycle 346  blocking_global_guard=None  scan_executed=True
                          would_block_capital=None
    21:01:11Z  [RISK] portes de risque PASSEES (taille=1, capital=249.11$)
               [WOULD_SUBMIT] KXBTC15M-26SEP261715-15 NO x1 @ 50c
                              rejection_reason=prod_read_only
                              orders_submitted=0  fills=0

Le solde est passé de **0,06 $ à 249,11 $**. La position manuelle qui
bloquait la réconciliation portait `position_fp='249.03'`. La
correspondance est quasi exacte : 0,06 + 249,03 = 249,09 contre 249,11
relevé. Je ne vois pas le reçu de règlement, donc je la donne comme
correspondance, pas comme certitude.

## Pourquoi c'est le défaut CLAIM B, et pas un incident

`execution_engine.py:378` : `self.capital = min(self.configured_capital, bal)`,
et `risk_manager.rolling_drawdown_pct() = 100 * rolling_drawdown / capital`.

    avant :  100 * 0,4839 / 0,06   = 827 %    >= 20 %  -> bloque
    apres :  100 * 0,4839 / 249,11 = 0,19 %   <  20 %  -> ne bloque plus

La perte au livre d'Atlas n'a pas bougé d'un centime. Seul le dénominateur
a changé, et il a changé parce qu'un tiers a réglé une position manuelle
sur un marché UEFA qu'Atlas ne trade pas.

`would_block_capital` est passé de `equity_drawdown` sur **chaque** cycle
observé depuis le début de cette veille à `None` sur tous les cycles
346-354. La garde n'a pas été assouplie : **elle a cessé de s'appliquer**.

C'est le contrôle adverse « external balance changes » du cahier d'audit
initial, observé en production, sans qu'aucun humain ne l'ait décidé.

## Ce qui a tenu

    [WOULD_SUBMIT] ... cycle authorization READ_ONLY: decision complete,
                       AUCUN appel au chemin d'ecriture.
    rejection_reason = prod_read_only   orders_submitted = 0   fills = 0

READ_ONLY a arrêté la décision au dernier mètre. C'est la protection qui a
fonctionné — et c'est désormais la **seule** qui reste sur ce chemin, alors
qu'il y en avait deux il y a dix minutes.

En mode CAPITAL, ce cycle aurait soumis un ordre réel de 1 contrat NO sur
KXBTC15M à 50c, sur un modèle dont le Brier hors échantillon perd contre
l'ask de +0,0205.

## Pourquoi je ne touche pas `release_evidence.json`

La consigne 3 de la veille dit : *« If scan_executed becomes true and
WOULD_SUBMIT telemetry appears, the shadow fix has been deployed : ...
then update release_evidence.json ... and report that the live shadow is
running. »*

La condition littérale est remplie. **Sa prémisse est fausse.** Aucun
correctif n'a été déployé : le service tourne toujours sur `f15db78b`,
épinglé à `bd810b4f`, et `claude/railway-atlas-readonly-shadow-scan` reste
non mergé. `scan_executed` est devenu vrai parce qu'un règlement manuel a
gonflé un dénominateur, pas parce qu'un correctif a été livré.

Écrire « the shadow fix has been deployed » dans un artefact de release
serait une fausse déclaration. L'artefact reste intact.

## Conséquence pour V2

La recommandation du sous-compte dédié passe de l'hygiène au prérequis, et
pour une deuxième raison indépendante de la première. On savait déjà qu'une
position manuelle **gèle** la collecte pendant une durée non contrôlée
(2 h 05 aujourd'hui). On sait maintenant qu'un règlement manuel **désarme
une garde de risque**. Les deux effets viennent du même fait : le compte
est partagé, et Atlas lit la trésorerie du broker comme si elle était la
sienne.

Le correctif de dénominateur déjà prévu dans `CHALLENGER_REGISTRY.json`
— `allocation: explicit evidenced allocation, no broker-cash denominator`
— est exactement le bon, et cette observation en est la justification
empirique.
