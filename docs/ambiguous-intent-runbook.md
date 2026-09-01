# Runbook — ambiguous intent alerts

An **ambiguous intent** is a submission whose POST left the process
without a usable answer. The ticker is locked fail-closed until the
engine can prove what happened. That lock is correct — and it is exactly
the kind of thing that goes unnoticed, because a locked ticker looks the
same as a market with no opportunities. This runbook is how a human
closes one.

**The rule that governs everything below: nothing here re-sends an
order.** Not the alerts, not the acknowledgement, not any step an
operator performs. The only paths out of an ambiguous intent are the
engine adopting an order it *found* at the broker (`FOUND`), or the
closure policy concluding absence after its confirmations
(`CLOSED_ABSENT`). See `live-canary-design.md` §3b.

## What you will see

Alerts are logged as `[INTENT_ALERT] <kind> severity=<...> ticker=<...>`
and persisted in `intent_alerts.json` (state and **age** survive
restarts, so a reboot never resets the clock).

| Kind | Severity | Meaning |
|---|---|---|
| `AMBIGUOUS_RESOLUTION_MULTIPLE` | CRITICAL | several orders carry the same `client_order_id` — a duplicate may already exist. Every submission, on every ticker, is halted. |
| `AMBIGUOUS_RESOLUTION_MALFORMED` | CRITICAL | the broker's order listing is unusable. Same global halt. |
| `AMBIGUOUS_LOOKUP_UNAVAILABLE_STREAK` | CRITICAL | N consecutive lookups failed: whether the order exists is still **unknown**. |
| `AMBIGUOUS_INTENT_STALE` | WARNING | an intent has stayed open past `AMBIGUOUS_INTENT_STALE_SECONDS` (default 900 s). |

`OrderManager.intent_health()` gives the live snapshot: open count,
oldest age, and per intent the `client_order_id`, status
(`NEVER_CHECKED` / `NOT_FOUND_PENDING` / `UNAVAILABLE` / `MULTIPLE` /
`MALFORMED`), the `NOT_FOUND` count, the last attempt timestamp, and the
age. No credential, key, signature or token appears in any of it — the
`client_order_id` is a deterministic hash of the order parameters, which
is precisely what you need to query the broker yourself.

## Notification externe

Un CRITICAL peut sortir du processus par un canal externe
(`alert_notifier.py`). Le transport est une abstraction: webhook HTTPS
aujourd'hui, e-mail ou Slack en implementant une seule methode, sans
toucher au moteur.

| Variable | Effet |
|---|---|
| `ALERT_WEBHOOK_URL` | active le canal (HTTPS obligatoire). Vide = livraison `SKIPPED`, jamais `FAILED` |
| `ALERT_WEBHOOK_TOKEN` | jeton du canal, transmis en en-tete `Authorization`, jamais dans le payload ni dans un log |
| `ALERT_NOTIFY_WARNINGS` | `true` pour notifier aussi les `STALE` (WARNING). Defaut: CRITICAL seulement |
| `ALERT_NOTIFY_MAX_ATTEMPTS` | plafond de reessais par alerte (defaut 5), compte cumule a travers les redemarrages |

Le payload contient ticker, `client_order_id`, statut, age, nombre de
tentatives, horodatage et detail — rien d'autre. Il est construit champ
par champ, donc un champ ajoute plus tard a une alerte ne part pas tout
seul sur le reseau.

Garanties, dans l'ordre d'importance:

* **Le canal ne peut pas trader.** Un notifier ne recoit qu'un
  dictionnaire plat; il ne voit ni le broker, ni le verrou, ni les
  intentions. Toute panne — timeout, 500, jeton manquant, bug interne —
  est absorbee: `notify_state=FAILED`, une trace, et rien d'autre. Aucune
  panne de notification ne peut provoquer `create_order`, `cancel_order`,
  une cloture d'intention ni un deverrouillage.
* **Une alerte est livree une fois**, pas a chaque cycle, et le
  dedoublonnage survit au redemarrage.
* **Les reessais de notification ne rejouent jamais rien cote broker.**
* `notify_state` (`SENT` / `FAILED` / `SKIPPED_NO_CHANNEL` /
  `SKIPPED_SEVERITY`), `notify_attempts`, `notified_at` et
  `notify_last_error` sont persistes avec l'alerte.

Une notification non livree n'est donc jamais une raison d'agir dans
l'urgence sur le moteur: le ticker est deja bloque fail-closed, et c'est
la procedure ci-dessous qui le debloque.

## Procedure

1. **Read the snapshot.** Note the ticker, the `client_order_id` and the
   status. Do not act on the alert text alone.
2. **Ask the broker directly**, with the same `client_order_id`, using
   the Kalshi portfolio order listing (`GET /portfolio/orders`, filter by
   ticker, match the id locally). This is a read; it changes nothing.
3. **Decide from what the broker says:**
   - **The order exists.** Let the engine adopt it: it does so on its own
     at the next resolution pass (submission path, periodic
     reconciliation, or restart). Nothing to do by hand. If it is
     resting and you want it gone, cancel it *at the broker*; the engine
     will reconcile.
   - **The order genuinely does not exist.** Do nothing and let the
     closure policy run: two complete, spaced readings close the intent
     and the ticker returns to the ordinary duplicate-guard rules. If you
     need it closed sooner, that is a deliberate operator decision — stop
     the engine, remove that ticker's entry from `pending_intents.json`,
     restart. Never edit the trade journal.
   - **`MULTIPLE`.** Identify every matching order at the broker and
     decide which, if any, to cancel — at the broker. Then clear the
     halt by restarting the engine once the listing is unambiguous.
   - **`MALFORMED` / `UNAVAILABLE` streak.** This is a broker or network
     problem, not a ledger problem. Wait for the listing to become
     readable; the engine retries on its own and stays locked meanwhile.
4. **Acknowledge** to record that a human is on it:
   `OrderManager.ack_intent_alert(ticker, operator, note)`. It stamps who
   and when, and stops the repeat logging for that alert. **It does not
   unlock the ticker, does not close the intent and cannot produce an
   order.** A new, distinct alert on the same ticker still fires.
5. **Confirm closure.** The alert clears by itself once its cause is
   gone — a cleared alert means the engine agrees, not that someone
   silenced it.

## What never to do

- Never re-submit "to see". The whole design exists because a re-send
  after an ambiguous POST is how the 2026-07-25 duplicate happened.
- Never clear an intent because the guard TTL expired. The TTL measures
  elapsed time; it is not evidence about the order.
- Never edit `kalshi_trades.json` to make an alert go away. Ledger
  corrections have their own append-only path
  (`docs/ledger-corrections.md`).
