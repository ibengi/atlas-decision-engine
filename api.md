# Surface API Kalshi utilisée

Base démo et production distinctes ; sélection par constructeur du client,
jamais par variable ambiante seule.

| Endpoint | Usage |
|---|---|
| GET /markets (cursor, series_ticker, status) | scan ciblé et crawl univers |
| GET /markets/{ticker} | re-lecture fraîche du book avant décision et exécution |
| GET /portfolio/balance | capital effectif (cents → $) |
| POST /portfolio/orders | ordre limite, `client_order_id` idempotent `alpha_<uuid>` |
| GET /portfolio/orders/{id} | suivi de statut (jamais suffisant seul) |
| DELETE /portfolio/orders/{id} | annulation TTL si non rempli |
| GET /portfolio/fills?order_id= | **source de vérité** des quantités/prix/frais |
| GET /portfolio/positions | réconciliation broker au démarrage |

Notes :
- retry avec backoff sur 429/5xx, timeout par défaut 15 s ;
- les frais renvoyés par l'API priment sur le barème local
  (`FeeModel.from_api`, `fee_source=api|model`) ;
- champs observés susceptibles de varier : vérifier les dumps `[RAW:*]`
  au premier déploiement.
