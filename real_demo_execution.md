# Preuve d'execution reelle sur Kalshi DEMO — mode d'emploi

## Pourquoi la preuve n'est pas jointe a cette livraison
L'environnement d'ingenierie ou ce code a ete ecrit **n'a pas d'acces
reseau a Kalshi** (proxy sortant : `403 Host not in allowlist:
demo-api.kalshi.co`, verifie le 2026-07-25) et **ne detient pas vos cles
DEMO**. Toute "preuve" produite ici serait donc, par construction, une
simulation — exactement ce que le cahier des charges interdit. Le
`demo_execution_proof.json` authentique ne peut etre genere que par VOUS,
en executant le script ci-dessous sur Railway (ou en local avec vos cles).

## Etape 1 — Variables Railway a ajouter/modifier
```
EXECUTION_MODE=real_demo            # active le garde anti-mock (FATAL sinon)
DRY_RUN=false
ALLOW_ORDER_SUBMISSION=true
SHADOW_MODE=0                       # real_demo refuse le shadow
ORDER_VERIFY_INTERVAL_SECONDS=3
ORDER_FILL_TIMEOUT_SECONDS=45
CANCEL_UNFILLED_ORDERS=true
# deja presentes normalement :
DEMO_TRADING=1
KALSHI_DEMO_KEY_ID=<votre key id demo>
KALSHI_DEMO_PRIVATE_KEY=<votre cle privee demo>
DATA_DIR=/data                      # volume persistant
# pour le script d'integration UNIQUEMENT (retirer apres) :
ENABLE_DEMO_INTEGRATION_TEST=true
```
Ne definissez PAS : LIVE_TRADING, KALSHI_ENV_CONFIRM (le script refuse
tout contexte LIVE).

## Etape 2 — Test A : preuve technique d'execution
Commande one-off Railway (ou locale) :
```
python scripts/kalshi_demo_execution_check.py
```
Le script : verifie l'URL DEMO codee en dur → solde → choisit le marche
ouvert le moins cher (ask ≤ 30c, spread ≤ 5c, series BTC) → soumet
1 contrat au ask → relit l'ordre (2e requete, meme kalshi_order_id) →
attend le fill (3 s d'intervalle, 45 s max) → verifie les fills → verifie
la position → ecrit `demo_execution_proof.json` → affiche
`[DEMO_EXECUTION_PROVED]` si tout est confirme par l'API.
Si l'ordre limite n'est pas croise dans le delai : annulation propre
`[ORDER_CANCELED_UNFILLED]` et rapport honnete (soumission/acceptation/
relecture prouvees, fill non obtenu) — relancer.

## Etape 3 — Test B : le moteur, sans rien forcer
```
python run.py --demo --loop
```
Les memes tags de preuve sont emis par le moteur a CHAQUE ordre reel :
`[ORDER_SUBMIT_ATTEMPT] → [ORDER_SUBMIT_RESPONSE] → [ORDER_VERIFY] →
[ORDER_WAITING_FOR_FILL]* → [FILL_VERIFY] → [ORDER_FILLED] →
[POSITION_VERIFY] → [POSITION_OPENED]`.
Aucun seuil n'a ete modifie : si aucune opportunite ne passe les portes,
le funnel du [CYCLE-SUMMARY] montre exactement quelle condition bloque.

## Sequence de logs Railway ATTENDUE (format, pas une preuve)
```
[EXECUTION]
environment=DEMO
api_base_url=https://demo-api.kalshi.co/trade-api/v2
execution_mode=REAL_DEMO
dry_run=false
mock_enabled=false
order_submission_enabled=true
NOTE: ordres reels sur l'API DEMO — fonds DEMO uniquement, aucun argent reel.
...
[ORDER_SUBMIT_ATTEMPT] ticker=KXBTCD-... side=yes action=buy count=1 price_cents=.. client_order_id=alpha_... environment=DEMO endpoint=/portfolio/orders
[ORDER_SUBMIT_RESPONSE] http_status=201 kalshi_order_id=<id Kalshi> client_order_id=alpha_... status=resting raw_response_sanitized={...}
[ORDER_VERIFY] kalshi_order_id=<le MEME id> status=resting remaining_count=1 filled_count=0
[ORDER_WAITING_FOR_FILL] elapsed_seconds=0 status=resting remaining_count=1
[FILL_VERIFY] kalshi_order_id=<id> fills_count=1 filled_contracts=1 average_fill_price=.. fees=..
[ORDER_FILLED] kalshi_order_id=<id> state=filled 1/1 @ ..c
[POSITION_VERIFY] ticker=... position_found=true net_position=1 ...
[POSITION_OPENED] ... confirme par l'API
[DEMO_EXECUTION_PROVED]        (script d'integration uniquement)
```

## Garanties verifiees par les tests (95/95)
- FakeClient/mock/patch en EXECUTION_MODE=real_demo → `[FATAL] Mock or
  simulation detected in REAL_DEMO mode` + exit 3 ;
- ordre accepte mais non rempli : JAMAIS marque filled, JAMAIS de
  POSITION_OPENED, annulation apres timeout ;
- relecture de l'ordre impossible → `[ORDER_VERIFY_FAILED]
  order_not_found_after_submission`, AUCUN trade enregistre ;
- position non retrouvee dans /positions → `position_found=false`,
  statut POSITION_OPENED NON emis ;
- refus HTTP journalise avec status + body, jamais avale ;
- cles/signatures/tokens absents de tous les logs (teste) ;
- le script refuse : flag manquant, contexte LIVE, URL non-DEMO.
