"""OrderManager — Gestion des ordres Kalshi (placement, suivi, annulation). Extrait de kalshi_alpha_bot.py (P3.8)."""
import hashlib
import json
import logging
import time

from config import CFG, _p, contract_cap_config
from execution_result import ExecutionResult
from fee_model import FeeModel
from kalshi_client import KalshiAPIError, KalshiClient, pick, pick_int
from persistence import JsonStore, PersistenceSentinel, verify_state_root
from position_manager import PositionManager
from trade_logger import TradeLogger, now_iso

log_api = logging.getLogger("API")


def _client_is_genuine(client) -> bool:
    return (type(client) is KalshiClient
            and str(getattr(client, "base_url", "")).startswith("https://")
            and "kalshi.co" in str(getattr(client, "base_url", "")))


def assert_real_demo_integrity(client, shadow_mode: bool):
    """Reject mocks and simulation when real-demo execution is requested."""
    if CFG.EXECUTION_MODE != "real_demo":
        return
    problems = []
    if not _client_is_genuine(client):
        problems.append(f"client non authentique: {type(client).__name__}")
    if CFG.DRY_RUN:
        problems.append("DRY_RUN=true")
    if shadow_mode:
        problems.append("SHADOW_MODE actif (= simulation)")
    if not CFG.ALLOW_ORDER_SUBMISSION:
        problems.append("ALLOW_ORDER_SUBMISSION=false")
    if problems:
        log_api.critical("[FATAL] Mock or simulation detected in REAL_DEMO mode: "
                         + "; ".join(problems))
        raise SystemExit(3)

class OrderManager:
    TERMINAL = {"executed", "canceled", "cancelled", "expired", "filled"}

    def __init__(self, client: KalshiClient):
        self.client = client
        # Continuite d'etat (LIVE-capable) : sous REQUIRE_PERSISTENT_STATE,
        # un repertoire d'etat neuf/efface arme la sentinelle AVANT toute
        # possibilite de soumission — le moteur ne reprend jamais le
        # trading comme sain sur un disque qui a perdu ses garanties.
        verify_state_root()
        self.open_orders = JsonStore.load(_p(CFG.ORDERS_FILE), {})  # id -> meta
        # Garde anti-doublon de SESSION, independante de l'enregistrement des
        # trades : un ordre soumis (201) sur un ticker verrouille ce ticker
        # pour la duree configuree, MEME si la verification echoue ensuite.
        # Motif : bug 2026-07-25 (GET V1 404 sur ordres V2) -> le meme signal
        # a ete re-soumis et REMPLI ~8 fois, solde debite sans aucun trade
        # local. Ce verrou rend ce mode de defaillance impossible.
        guard_raw = JsonStore.load(_p("submission_guard.json"), {})
        now = time.time()
        self.session_submitted = {
            str(tk): float(ts) for tk, ts in (guard_raw or {}).items()
            if isinstance(ts, (int, float)) and
            (CFG.SUBMIT_DEDUP_TTL_S <= 0 or now - float(ts) < CFG.SUBMIT_DEDUP_TTL_S)
        }                                   # ticker -> epoch de soumission
        # Cooldown apres 503 exchange (demo en pause/maintenance) : inutile
        # et bruyant de marteler l'API a chaque cycle.
        self.exchange_pause_until = 0.0
        log_api.info(f"[SUBMISSION_GUARD_LOADED] active_tickers={len(self.session_submitted)} "
                     f"ttl_seconds={CFG.SUBMIT_DEDUP_TTL_S:.0f}")

    def flush(self):
        JsonStore.save(_p(CFG.ORDERS_FILE), self.open_orders)
        self._flush_submission_guard()

    def _flush_submission_guard(self):
        """Persiste le verrou anti-doublon afin qu'un redemarrage Railway
        ne permette pas de resoumettre le meme ticker pendant le TTL."""
        now = time.time()
        if CFG.SUBMIT_DEDUP_TTL_S > 0:
            self.session_submitted = {
                tk: ts for tk, ts in self.session_submitted.items()
                if now - ts < CFG.SUBMIT_DEDUP_TTL_S
            }
        JsonStore.save(_p("submission_guard.json"), self.session_submitted)

    # -- extraction tolerante de l'etat d'un ordre ---------------------------
    @staticmethod
    def _extract(order: dict, requested: int):
        status = str(pick(order, "status", "order_status", default="") or "").lower()
        # Formats numeriques ET a virgule fixe *_fp ("2.00") reellement
        # renvoyes par l'API (fixture utilisateur) -- D1.
        filled = pick_int(order, "taker_fill_count", "fill_count", "fill_count_fp",
                          "filled_count", "filled_quantity", default=-1)
        remaining = pick_int(order, "remaining_count", "remaining_count_fp",
                             "remaining_quantity", default=-1)
        if filled < 0 and remaining >= 0:
            filled = max(0, requested - remaining)
        # D2 : un statut "executed" seul ne vaut JAMAIS confirmation de fill.
        # La confirmation passe par les compteurs ci-dessus ou, en dernier
        # recours, par /portfolio/fills (voir place_and_track).
        if filled < 0:
            filled = 0
        return status, min(filled, requested)

    def _avg_fill_price(self, order_id: str, side: str, fallback: int,
                        fills: list = None) -> int:
        # CORRECTIF AUDIT : accepte des fills deja recuperes pour eviter
        # un second appel a /portfolio/fills pour le meme order_id.
        if fills is None:
            try:
                fills = self.client.get_fills(order_id)
            except KalshiAPIError:
                fills = []
        tot_c, tot_px = 0, 0
        for f in fills:
            c  = pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
            raw = pick(f, f"{side}_price_dollars", "price_dollars",
                       f"{side}_price", "price", default=None)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            # v >= 1 -> deja en cents ; v < 1 -> en dollars ("0.1200" = 12c)
            px = int(round(v)) if v >= 1 else int(round(v * 100))
            if c > 0 and 1 <= px <= 99:
                tot_c += c; tot_px += c * px
        if tot_c > 0:
            return round(tot_px / tot_c)
        log_api.warning(f"Fills indisponibles pour {order_id} -- prix moyen "
                        f"suppose = prix limite ({fallback}c).")
        return fallback

    @staticmethod
    def _client_order_id(ticker: str, side: str, count: int,
                         limit_cents: int) -> str:
        """Return the stable Kalshi id used to make order retries idempotent."""
        idempotency_key = f"{ticker}|{side}|{count}|{limit_cents}"
        return f"alpha_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"

    # -- cycle de vie complet d'un ordre --------------------------------------
    def place_and_track(self, ticker: str, side: str, count: int,
                        limit_cents: int) -> ExecutionResult:
        # INVARIANT DUR : aucune ecriture broker apres une panne de
        # persistance critique. Si le verrou anti-doublon, le journal ou
        # l'etat des ordres ne peut plus etre ecrit, chaque garantie de
        # redemarrage est deja morte — soumettre serait s'exposer au
        # doublon 2026-07-25 en sachant que la protection n'existe plus.
        if not PersistenceSentinel.healthy():
            f = PersistenceSentinel.failure() or {}
            log_api.error("[ORDER_SUBMIT_ATTEMPT] bloque fail-closed: panne "
                          f"de persistance critique ({f.get('path')}: "
                          f"{f.get('reason')}) -- create_order NON appele.")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:persistence_failure", "rejected")
        # INVARIANT DUR : plafond de contrats independant du sizing.
        # 1) Configuration du cap invalide/absente (LIVE-capable) : erreur
        #    operateur VISIBLE et soumissions desactivees fail-closed —
        #    jamais de reinterpretation silencieuse.
        live_capable = (CFG.REQUIRE_PERSISTENT_STATE
                        or getattr(self.client, "env", "demo") == "prod")
        cap, cap_err = contract_cap_config(live_capable=live_capable)
        if cap is None:
            log_api.error(f"[CONFIG_INVALID] {cap_err} -- create_order NON "
                          f"appele (fail-closed).")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:contract_cap_invalid", "rejected")
        # 2) Un count superieur au cap signifie un bug de dimensionnement en
        #    amont ; on BLOQUE (pas de clamp silencieux) pour le rendre
        #    visible.
        if count > cap:
            log_api.error(f"ORDRE BLOQUE (invariant): {ticker} count={count} "
                          f"> MAX_CONTRACTS_PER_ORDER={cap} "
                          f"-- create_order NON appele.")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:contract_cap_exceeded", "rejected")
        # INVARIANT DUR : aucun create_order sans prix executable valide.
        # Derniere ligne de defense contre un carnet vide qui aurait
        # traverse scanner, ranker et validateur.
        if limit_cents is None or not isinstance(limit_cents, (int, float)) \
                or not (1 <= int(limit_cents) <= 99) \
                or side not in ("yes", "no") or count <= 0:
            log_api.error(f"ORDRE BLOQUE (invariant): {ticker} side={side} "
                          f"count={count} limite={limit_cents!r} -- "
                          f"prix/ask invalide, create_order NON appele.")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:no_executable_ask", "rejected")
        limit_cents = int(limit_cents)
        assert_real_demo_integrity(self.client, CFG.SHADOW_MODE)
        if not CFG.ALLOW_ORDER_SUBMISSION:
            log_api.error("[ORDER_SUBMIT_ATTEMPT] bloque: "
                          "ALLOW_ORDER_SUBMISSION=false")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:submission_disabled", "rejected")
        now = time.time()
        if now < self.exchange_pause_until:
            log_api.warning("[ORDER_SUBMIT_SKIPPED] exchange en cooldown "
                            f"apres 503 (reprise dans "
                            f"{self.exchange_pause_until - now:.0f}s)")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:exchange_unavailable_cooldown",
                                   "rejected")
        last = self.session_submitted.get(ticker)
        if last is not None and now - last < CFG.SUBMIT_DEDUP_TTL_S:
            log_api.warning("[ORDER_SUBMIT_SKIPPED] garde anti-doublon: un "
                            f"ordre a deja ete soumis sur {ticker} il y a "
                            f"{now - last:.0f}s (TTL "
                            f"{CFG.SUBMIT_DEDUP_TTL_S:.0f}s) -- re-soumission "
                            "refusee meme si le trade local est absent.")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:duplicate_submission_guard",
                                   "rejected")
        client_order_id = self._client_order_id(ticker, side, count, limit_cents)
        env_name = "DEMO" if getattr(self.client, "env", "demo") == "demo" \
            else "PROD"
        # Exigence 4 : preuve d'envoi AVANT l'appel. Jamais de cle, secret,
        # signature ni token dans les logs (seuls les champs metier).
        v2_side = "bid" if side == "yes" else "ask"
        v2_price = (limit_cents if side == "yes"
                    else 100 - limit_cents) / 100.0
        log_api.info("[ORDER_SUBMIT_ATTEMPT] "
                     f"ticker={ticker} side={side} action=buy count={count} "
                     f"price_cents={limit_cents} "
                     f"(V2: side={v2_side} price={v2_price:.4f}) "
                     f"client_order_id={client_order_id} "
                     f"environment={env_name} "
                     f"endpoint="
                     f"{getattr(self.client, 'ORDERS_V2_PATH', '/portfolio/events/orders')}")
        try:
            order = self.client.create_order(ticker, side, count, limit_cents,
                                             client_order_id=client_order_id)
        except KalshiAPIError as e:
            log_api.error("[ORDER_SUBMIT_FAILED] "
                          f"http_status={e.status} error_code={e.status} "
                          f"error_message={e} "
                          f"response_body_sanitized={str(e.body)[:300]}")
            if getattr(e, "status", None) == 503:
                self.exchange_pause_until = time.time() + CFG.EXCHANGE_503_COOLDOWN_S
                log_api.warning("[EXCHANGE_COOLDOWN] 503 exchange -- pause "
                                f"des soumissions {CFG.EXCHANGE_503_COOLDOWN_S:.0f}s "
                                "(demo probablement en maintenance).")
            return ExecutionResult(None, count, 0, limit_cents, f"rejected:{e.status}",
                                   "rejected")
        self.exchange_pause_until = 0.0
        self.session_submitted[ticker] = time.time()
        # Persistance IMMEDIATE : meme un crash entre le HTTP 201 et
        # l'enregistrement local ne peut plus autoriser un doublon.
        self._flush_submission_guard()
        http_status = getattr(self.client, "last_http_status", None)
        order_id = str(pick(order, "order_id", "id", default="") or "")
        log_api.info("[ORDER_SUBMIT_RESPONSE] "
                     f"http_status={http_status} "
                     f"request_id={order.get('request_id', '-')} "
                     f"kalshi_order_id={order_id or '-'} "
                     f"client_order_id={client_order_id} "
                     f"status={pick(order, 'status', 'order_status', default='-')} "
                     f"raw_response_sanitized="
                     f"{json.dumps(order, default=str)[:400]}")
        # Exigence 5 : relecture IMMEDIATE depuis l'API. Tant qu'elle n'a
        # pas reussi, l'ordre n'est PAS considere comme confirme.
        if order_id:
            try:
                verified = self.client.get_order(order_id)
            except KalshiAPIError as e:
                verified = {}
                log_api.warning(f"[ORDER_VERIFY] relecture en erreur: {e}")
                # BUG CORRIGE (logs 2026-07-25 23:14+) : les ordres crees via
                # V2 repondent 404 sur le GET V1 -> tous les fills REELS
                # etaient rejetes "unverified", aucun trade enregistre, et le
                # MEME signal etait re-soumis chaque cycle (solde debite en
                # silence). La reponse de creation V2 vient du moteur
                # d'appariement (order_id, fill_count, remaining_count,
                # ts_ms) : c'est une source de verite valide. On l'utilise
                # comme verification quand le GET est indisponible ; la
                # confirmation finale reste FILL_VERIFY + POSITION_VERIFY.
                if getattr(e, "status", None) == 404 and (
                        order.get("ts_ms") or "fill_count" in
                        (order.get("raw") or {})):
                    verified = order
                    log_api.info("[ORDER_VERIFY] "
                                 "source=create_response_v2 (GET ordre "
                                 "indisponible pour les ordres V2 -- etat "
                                 "certifie par la reponse du moteur "
                                 f"d'appariement, ts_ms={order.get('ts_ms')})")
            if verified:
                v_status, v_filled = self._extract(verified, count)
                log_api.info("[ORDER_VERIFY] "
                             f"kalshi_order_id={order_id} status={v_status} "
                             f"remaining_count={pick_int(verified, 'remaining_count', default=-1)} "
                             f"filled_count={v_filled} "
                             f"yes_price={verified.get('yes_price', '-')} "
                             f"no_price={verified.get('no_price', '-')}")
                order = verified
            else:
                log_api.error("[ORDER_VERIFY_FAILED] "
                              "reason=order_not_found_after_submission")
                return ExecutionResult(order_id, count, 0, limit_cents,
                                       "unverified", "rejected")
        if not order_id:
            log_api.error(f"Reponse d'ordre sans identifiant -- trade NON enregistre. "
                          f"Reponse: {json.dumps(order)[:300]}")
            return ExecutionResult(None, count, 0, limit_cents, "no_id", "rejected")

        self.open_orders[order_id] = {"ticker": ticker, "side": side,
                                      "count": count, "price": limit_cents,
                                      "placed_at": now_iso()}
        self.flush()

        start = time.time()
        deadline = start + CFG.ORDER_TTL_SECONDS
        delay = max(2.0, min(5.0, CFG.ORDER_VERIFY_INTERVAL_S))
        status, filled = self._extract(order, count)
        get_order_available = True     # 404 V2 : suivi via /fills a la place
        while time.time() < deadline and status not in self.TERMINAL and filled < count:
            log_api.info("[ORDER_WAITING_FOR_FILL] "
                         f"elapsed_seconds={time.time() - start:.0f} "
                         f"status={status or 'resting'} "
                         f"remaining_count={count - filled}")
            time.sleep(delay)
            try:
                if get_order_available:
                    order = self.client.get_order(order_id)
                    status, filled = self._extract(order, count)
                else:
                    raise KalshiAPIError(404, "get_order indisponible (V2)")
            except KalshiAPIError as e:
                if getattr(e, "status", None) == 404:
                    get_order_available = False
                    try:            # source de verite alternative : /fills
                        fills = self.client.get_fills(order_id)
                        got = sum(pick_int(fl, "count", "count_fp", "quantity", "quantity_fp", default=0)
                                  for fl in fills)
                        if got > filled:
                            filled = min(count, got)
                        if filled >= count:
                            status = "executed"
                    except KalshiAPIError as e2:
                        log_api.warning(f"Suivi ordre {order_id} via fills: {e2}")
                else:
                    log_api.warning(f"Suivi ordre {order_id}: {e}")

        if status not in self.TERMINAL and filled < count:
            if not CFG.CANCEL_UNFILLED_ORDERS:
                log_api.info(f"[ORDER_STILL_RESTING] {order_id} "
                             f"(CANCEL_UNFILLED_ORDERS=false)")
                return ExecutionResult(order_id, count, filled, limit_cents,
                                       status or "resting", "resting")

            pre_cancel_filled = filled
            log_api.info("[ORDER_CANCEL_ATTEMPT] "
                         f"kalshi_order_id={order_id} "
                         f"timeout_seconds={CFG.ORDER_TTL_SECONDS} "
                         f"known_filled={filled}/{count}")
            try:
                cancel_resp = self.client.cancel_order(order_id)
                reduced_by = pick_int(cancel_resp, "reduced_by", "reduced_count", default=-1)
                # Reconciliation obligatoire apres annulation : les fills ont
                # pu augmenter entre le dernier poll et le DELETE.
                fills_after = self.client.get_fills(order_id, strict=True)
                filled_after = min(count, sum(
                    pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
                    for f in fills_after))
                filled = max(pre_cancel_filled, filled_after)
                expected_reduction = max(0, count - filled)
                if reduced_by < 0 or reduced_by < expected_reduction:
                    raise KalshiAPIError(
                        0,
                        f"annulation non prouvee: reduced_by={reduced_by}, "
                        f"reste_attendu={expected_reduction}",
                        json.dumps(cancel_resp))
                status = "canceled"
                log_api.info("[ORDER_CANCEL_CONFIRMED] "
                             f"kalshi_order_id={order_id} reduced_by={reduced_by} "
                             f"final_filled={filled}/{count}")
            except KalshiAPIError as e:
                # FAIL-CLOSED : conserver l'ordre dans orders_state.json et
                # refuser de pretendre qu'il est annule. Le garde anti-doublon
                # reste actif et la reconciliation de demarrage reprendra.
                meta = self.open_orders.get(order_id, {})
                meta.update({"state": "unknown_cancel_failed",
                             "last_error": str(e),
                             "last_checked_at": now_iso(),
                             "known_filled": filled})
                self.open_orders[order_id] = meta
                self.flush()
                log_api.error("[ORDER_CANCEL_FAILED] "
                              f"kalshi_order_id={order_id} error={e} "
                              "state=UNKNOWN trading_for_ticker_halted=true")
                return ExecutionResult(order_id, count, filled, limit_cents,
                                       "unknown_cancel_failed", "unknown")

        self.open_orders.pop(order_id, None); self.flush()

        # Confirmation par l'endpoint des fills (jamais par le statut seul) :
        # si le statut pretend "executed"/"filled" sans compteur exploitable,
        # on interroge /portfolio/fills, source de verite.
        if filled <= 0 and status in ("executed", "filled"):
            try:
                fills = self.client.get_fills(order_id)
            except KalshiAPIError as e:
                log_api.warning(f"/fills indisponible pour {order_id}: {e}")
                fills = []
            filled = min(count, sum(pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
                                    for f in fills))
            if filled > 0:
                log_api.info(f"Ordre {order_id}: statut '{status}' sans compteur "
                             f"-- {filled} contrat(s) confirme(s) via /fills.")

        if filled <= 0:
            log_api.info("[FILL_VERIFY] "
                         f"kalshi_order_id={order_id} fills_count=0 "
                         f"filled_contracts=0 average_fill_price=- fees=- "
                         f"(ordre accepte par l'API mais AUCUN fill — "
                         f"accepted != filled)")
            return ExecutionResult(order_id, count, 0, limit_cents,
                                   status or "unfilled", "cancelled")
        try:
            fills = self.client.get_fills(order_id)
        except KalshiAPIError as e:
            log_api.warning(f"/fills indisponible pour {order_id}: {e} -- "
                            "frais/prix moyens estimes depuis la reponse de "
                            "creation V2.")
            fills = []
        # CORRECTIF AUDIT : le calcul local de "fees" ici a ete supprime --
        # il n'etait jamais transmis a l'appelant (ExecutionResult n'a pas
        # de champ fees) et sommait le champ "fees" des fills SANS la
        # conversion cents/dollars appliquee ailleurs par FeeModel._amount,
        # ce qui aurait pu donner un montant errone de x100 si jamais
        # utilise. Le frais REEL est calcule une seule fois, correctement,
        # dans _execute_decision via FeeModel.from_api. On reutilise les
        # `fills` deja recuperes ci-dessus pour le prix moyen (evite un
        # second appel a /portfolio/fills pour le meme ordre).
        avg = self._avg_fill_price(order_id, side, limit_cents, fills=fills)
        log_api.info("[FILL_VERIFY] "
                     f"kalshi_order_id={order_id} fills_count={len(fills)} "
                     f"filled_contracts={filled} average_fill_price={avg}")
        state = "filled" if filled >= count else "partial"
        log_api.info(f"[ORDER_FILLED] kalshi_order_id={order_id} "
                     f"state={state} {filled}/{count} @ {avg}c")
        return ExecutionResult(order_id, count, filled, avg, status or "executed", state)

    def reconcile_startup(self, tlog: TradeLogger, posmgr: PositionManager):
        """Recovery apres crash avec politique fail-closed.

        Un ordre n'est retire du journal local que si son etat est resolu.
        Si GET ordre est indisponible, /fills puis l'annulation V2 servent de
        sources alternatives. Un ordre encore incertain reste persiste pour
        le prochain cycle au lieu d'etre oublie.
        """
        if not self.open_orders:
            return
        log_api.warning(f"Recovery: {len(self.open_orders)} ordre(s) non conclu(s) "
                        "au dernier arret -- reconciliation...")
        for oid, meta in list(self.open_orders.items()):
            resolved = False
            status, filled = "", 0
            try:
                try:
                    order = self.client.get_order(oid)
                    status, filled = self._extract(order, meta["count"])
                except KalshiAPIError as e:
                    if e.status != 404:
                        raise
                    fills = self.client.get_fills(oid)
                    filled = min(meta["count"], sum(
                        pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0) for f in fills))
                    status = "executed" if filled >= meta["count"] else "unknown"

                if status not in self.TERMINAL and filled < meta["count"]:
                    cancel_resp = self.client.cancel_order(oid)
                    reduced_by = pick_int(cancel_resp, "reduced_by", "reduced_count", default=-1)
                    fills = self.client.get_fills(oid, strict=True)
                    filled = min(meta["count"], sum(
                        pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
                        for f in fills))
                    expected_reduction = max(0, meta["count"] - filled)
                    if reduced_by < 0 or reduced_by < expected_reduction:
                        raise KalshiAPIError(
                            0,
                            f"recovery cancel non prouve: reduced_by={reduced_by}, "
                            f"reste_attendu={expected_reduction}",
                            json.dumps(cancel_resp))
                    status = "partial" if filled > 0 else "cancelled"
                    log_api.info("[RECOVERY_CANCEL_CONFIRMED] "
                                 f"kalshi_order_id={oid} reduced_by={reduced_by} "
                                 f"final_filled={filled}/{meta['count']}")
                    resolved = True
                else:
                    resolved = True

                if filled > 0 and not tlog.has_open_on(meta["ticker"]):
                    avg = self._avg_fill_price(oid, meta["side"], meta["price"])
                    fills = self.client.get_fills(oid)
                    fees, _fee_src = FeeModel.from_api({}, fills, filled, avg)
                    t = tlog.open_trade(
                        ticker=meta["ticker"], market_title="(recovery)",
                        side=meta["side"], req_price=meta["price"], avg_price=avg,
                        req_count=meta["count"], filled_count=filled,
                        spread=None, fees=fees, edge=0.0, ev=0.0, confidence=0,
                        grade="R", reason="ordre recupere apres crash",
                        analysis={"fee_source": _fee_src}, order_id=oid,
                        order_status=status)
                    posmgr.open_position(t)
            except KalshiAPIError as e:
                log_api.error(f"Recovery ordre {oid}: {e} -- ordre conserve localement")
                resolved = False
            except Exception as e:
                log_api.exception(f"Recovery ordre {oid}: {e} -- ordre conserve localement")
                resolved = False
            if resolved:
                self.open_orders.pop(oid, None)
        self.flush()
