# Audit du candidat `7621e8c` — clôture du conflit de protocole

Conduit le 2026-09-26. Je ne suis pas l'auteur. Rien n'a été amélioré ni
réglé. Aucun déploiement, merge, approbation, promotion ni réécriture de
ledger.

## Verdict

    CANDIDATE_AUDIT_SURVIVES

Chaque revendication vérifiable a été vérifiée, et aucune n'a cédé. Mais la
clôture **porte sur le dépôt, pas sur ce qui tourne** — et c'est ce qui
tourne qui consomme des jours à minuit. Voir BLOCKER-1.

Un audit survécu n'est pas une autorisation de capital.

## Ce que j'ai recalculé, pas lu

| revendication | méthode | résultat |
|---|---|---|
| MR inchangé : dates, deux familles, multiplicité | sha256 de `CHALLENGER_REGISTRY.json` à `f322cb6` vs `7621e8c` | **octet-identique** `4c2983a6…` |
| charge utile et hash PHASE2 préservés | recalcul `sha256(json.dumps(protocol, sort_keys, separators compacts))` | **`751652ff…` MATCH** ; `protocol` identique à l'octet |
| hashs auto-pinnés du module | recalcul de `MR_HASH` et `CATALOG_HASH` | **les deux MATCH** |
| 175 tests | relancés dans un worktree au SHA exact | **175 passed, 118 subtests** |
| 88 mutations tuées | `v2/mutate.py` relancé | **88/88, 0 survivante**, dont 12 sur l'autorité |
| CI sur le SHA exact | run 36264263898 | `head_sha=7621e8ce…`, **success**, PR 80 |
| PR #80 draft/non mergé | API GitHub | `draft:true, merged:false`, base `atlas-v2/rebuild` |
| coûts manquants ⇒ rentabilité indisponible | lecture du code | *"Missing, malformed or negative costs remain unavailable, never zero"* — le défaut qu'on m'avait reproché est corrigé |
| non déployé | **les cinq** services Railway | `7621e8c` déployé nulle part |

## Mes sondes adverses sur le fail-closed

Quatorze cas que les tests d'Astra ne couvraient pas nécessairement.
Treize se comportent correctement :

    deux ACTIVE, meme marche, fenetres ouvertes   -> PROTOCOL_AUTHORITY_CONFLICT
    statut inconnu / minuscule / avec espace      -> refuse (fail-closed)
    marches vides / fenetres vides                -> refuse
    fenetre inversee / degeneree                  -> refuse
    chevauchement d'un seul instant               -> refuse
    fenetres contigues bord a bord                -> accepte (correct)
    marches disjoints / fenetres disjointes       -> accepte (correct)

Le défaut par défaut est bien orienté : si `authority_status` disparaissait
de `TRAINING_PROTOCOL.json`, PHASE2 redeviendrait `ACTIVE`, deux autorités
se chevaucheraient et l'ensemble refuserait. Le mauvais cas mène au refus.

## BLOCKER-1 — la clôture n'agit pas là où le conflit vit

`atlas-v2-data` (service `e8952903`) tourne, épinglé à
`55cd4530dc4fc143f9c041eceaada9a1ba88bef4`, **antérieur au correctif** :

    protocol_authority.py           ABSENT
    PROTOCOL_AUTHORITY.json         ABSENT
    TRAINING_PROTOCOL.authority_status   <ABSENT>
    TRAINING_PROTOCOL.superseded_by      <ABSENT>

Et il exécute PHASE2 en ce moment. Relevé à 19:03:04Z :

    mode                             LIVE_MARKET_LEARNING
    learner_enabled                  true       activated_at 13:50:06Z
    phase2.status                    COLLECTING_FROZEN_TRAINING_DATA
    phase2.protocol_version          PHASE2-20260926-1
    phase2.protocol_hash             751652ff…  (le protocole supersédé)
    phase2.eligible_not_before       2026-09-27T00:00:00Z
    phase2.qualified_predictive_decisions   0
    phase2.batch_count               0
    decisions 105 / settled 105 / eligible_predictive 63

La revendication d'Astra — *candidate code; not deployed* — est
littéralement exacte et honnêtement formulée. La conséquence, elle, n'est
pas dite : **`PROTOCOL_CONFLICT_CLOSED` décrit le dépôt, alors que le
conflit se trouve dans le déploiement**, et le déploiement commence à
qualifier des lignes PHASE2 sur KXBTC15M à `2026-09-27T00:00:00Z` — le
marché et l'instant exacts que MR revendique en autorité unique.

Ce qui est préservé : `assert_row` de MR exige `source_protocol_id == MR`
et `prior_candidate_uses == []`, donc des lignes PHASE2 ne peuvent pas être
réétiquetées en MR. Ce qui ne l'est pas : deux protocoles observant **les
mêmes événements sur les mêmes jours**, dont un lecteur humain lira les
deux résultats. C'est une multiplicité non déclarée — le constat d'origine,
déplacé du dépôt vers le runtime.

Ce qui joue en faveur : `qualified_predictive_decisions = 0` et
`batch_count = 0`. **Rien n'a encore été consommé.** La fenêtre pour agir
sans perte est ouverte jusqu'à minuit.

Trois issues, toutes décisions du propriétaire, aucune de l'auditeur :
déployer le correctif sur ce service ; arrêter la qualification PHASE2
avant `eligible_not_before` ; ou laisser PHASE2 courir et **écrire** la
multiplicité conjointe sur les sept familles avant que le premier jour ne
soit dépensé. Ne rien faire revient à choisir la troisième sans l'écrire.

## OBSERVATION-1 — normalisation des marchés (latent)

`assert_no_overlap` compare les marchés par égalité de chaîne exacte
(`set(a['markets']) & set(b['markets'])`). Une variante de casse rend deux
autorités **mutuellement invisibles** :

    MR 'KXBTC15M' vs autre 'kxbtc15m'   -> ACCEPTE (aucun conflit détecté)

Non exploitable aujourd'hui : j'ai falsifié le catalogue et le pin
`CATALOG_HASH` l'a refusé (`2000ac38…` ≠ `94c68ee0…`), et
`assert_no_overlap` n'est appelé que depuis `authority()` sur des claims
pinnées. Le trou s'ouvre le jour où le catalogue est légitimement mis à
jour — ajout d'un marché ou d'un protocole. C'est la classe exacte du
`canonical_ticker()` de V1, où la défense existait côté ticker et manque
ici côté marché.

## MINEUR-1 — collision de chemin

`tools/shadow_pnl.py` existe en deux versions différentes au même chemin :
la mienne (`a375a548…`, branche d'audit) et celle d'Astra (`fc08718c…`,
`7621e8c`). Résolution triviale — prendre celle d'Astra, qui corrige le
défaut de coût — mais à trancher avant tout merge.

## Reste ouvert

`status=PREREGISTERED_NOT_TRAINED`, les deux challengers `NOT_TESTABLE`,
aucun lock persisté. Le contre-audit du candidat MR lui-même reste dû quand
il existera. État financier vérifié inchangé : `CAPITAL=OFF`,
`broker_writes=0`, `real_orders_submitted=0`, `model_approved=false`,
`auto_promotion=false`, production principale en READ_ONLY.
