"""
market_scanner.py — v2 (2026-07-24)
Scanner d'univers Kalshi : CIBLE d'abord, crawl complet EN CACHE.

CAUSES RACINES CORRIGEES (logs du 2026-07-20) :
  - 40 001 marches re-scannes CHAQUE minute (cycle ~75 s dont ~14 s de
    pagination) ;
  - garde-fou MAX_PAGES=200 atteint a CHAQUE cycle (univers tronque,
    alerte repetee 31 fois en 31 cycles) ;
  - ~29 400 marches SANS liquidite re-telecharges et re-evalues a chaque
    cycle.

v2 :
  1. Scan CIBLE par series derivees du registre de strategies (quelques
     pages par cycle, via GET /markets?series_ticker=...). C'est le chemin
     par defaut : on ne re-telecharge JAMAIS l'univers entier chaque minute.
  2. Crawl complet OPTIONNEL (SCANNER_GENERAL_CRAWL_ENABLED) mis en cache
     >= 30 min (SCANNER_UNIVERSE_TTL_S), persiste sur disque, mise a jour
     INCREMENTALE entre deux rafraichissements (les series prioritaires
     seules sont re-lues). L'alerte MAX_PAGES ne peut plus apparaitre
     qu'une fois par rafraichissement, PAS par cycle — et MAX_PAGES n'a
     PAS ete augmente.
  3. Filtres IMMEDIATS pendant le scan : expire/ferme, hors fenetre
     temporelle, sans liquidite, market_type inconnu non tradable.
     Metriques avant/apres CHAQUE filtre dans le rapport.
  4. Plafond d'evaluation couteuse : seuls les SCANNER_MAX_MARKETS_PER_CYCLE
     meilleurs marches tradables sortent du scanner.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

from market_classifier import classify

log = logging.getLogger("SCANNER")

SCANNER_VERSION = "scanner-v2-2026-07-24"


def _env_f(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_i(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_b(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    log.warning(f"get_env_bool: valeur '{v}' ignoree pour {name} "
                f"(defaut={default})")
    return default


# Series par market_type — derivation du perimetre CIBLE depuis le registre.
DEFAULT_SERIES_BY_MARKET_TYPE = {
    "btc_15m_above_strike":  ["KXBTC15M"],
    "btc_above_strike_daily": ["KXBTCD"],
    "sports_moneyline": ["KXWNBAGAME", "KXMLBGAME", "KXNBAGAME", "KXNFLGAME",
                         "KXNHLGAME", "KXCFLGAME", "KXATPMATCH", "KXWTAMATCH",
                         "KXITFWMATCH", "KXATPCHALLENGERMATCH"],
    "sports_spread": ["KXWNBASPREAD", "KXMLBSPREAD", "KXNBASPREAD",
                      "KXNFLSPREAD", "KXATPGSPREAD"],
    "sports_total":  ["KXWNBATOTAL", "KXMLBTOTAL", "KXNBATOTAL",
                      "KXNFLTOTAL"],
    "election_winner": ["KXPRES", "KXSENATE"],
}


class ScannerConfig:
    def __init__(self):
        self.general_crawl = _env_b("SCANNER_GENERAL_CRAWL_ENABLED", False)
        raw = os.getenv("SCANNER_PRIORITY_SERIES", "").strip()
        self.priority_series = [s.strip().upper() for s in raw.split(",")
                                if s.strip()] if raw else None
        self.priority_max_pages = _env_i("SCANNER_PRIORITY_MAX_PAGES", 5)
        self.max_markets_per_cycle = _env_i("SCANNER_MAX_MARKETS_PER_CYCLE", 300)
        self.lookahead_hours = _env_f("SCANNER_LOOKAHEAD_HOURS", 24)
        self.min_minutes = _env_f("SCANNER_MIN_MINUTES", 5)
        self.universe_ttl_s = max(1800, _env_i("SCANNER_UNIVERSE_TTL_S", 1800))
        self.crawl_max_pages = _env_i("SCANNER_CRAWL_MAX_PAGES", 200)
        self.page_limit = _env_i("SCANNER_PAGE_LIMIT", 200)


def _minutes_to_close(m: dict, now_dt: datetime):
    ct = m.get("close_time")
    if not ct:
        return None
    try:
        close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (close_dt - now_dt).total_seconds() / 60.0


def _has_liquidity(m: dict) -> bool:
    """Un cote executable au moins (prix 1..99 sur un ask), OU volume/
    open_interest > 0. Kalshi renvoie 0 pour un carnet vide."""
    for k in ("yes_ask", "no_ask", "yes_bid", "no_bid"):
        try:
            v = int(float(m.get(k)))
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 99:
            return True
    for k in ("volume", "volume_24h", "open_interest", "liquidity"):
        try:
            if float(m.get(k) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


class MarketScanner:
    """series_provider() -> liste de series cibles (derivee du registre si
    non fournie). client doit exposer get_markets(series,...) et _req()."""

    def __init__(self, client, router=None, cfg: ScannerConfig = None,
                 data_dir: str = ".", now_fn=None):
        self.client = client
        self.router = router
        self.cfg = cfg or ScannerConfig()
        self.data_dir = data_dir
        self.now_fn = now_fn or time.time
        self._universe = None            # liste de marches (crawl complet)
        self._universe_ts = 0.0
        self._crawl_pages_last = 0
        self._cache_path = os.path.join(data_dir, "universe_cache.json")
        self._load_disk_cache()

    # ── perimetre cible ────────────────────────────────────────────────────
    def target_series(self) -> list:
        if self.cfg.priority_series:
            return self.cfg.priority_series
        series = []
        # Perimetre = types dont la strategie a une SOURCE de probabilite
        # (Tache 4/6 : ne jamais telecharger un marche qui sera rejete
        # no_model_probability ; s'etend automatiquement quand un
        # fournisseur sports/election est branche).
        if self.router and hasattr(self.router, "tradeable_market_types"):
            supported = self.router.tradeable_market_types()
        elif self.router:
            supported = self.router.supported_market_types()
        else:
            supported = DEFAULT_SERIES_BY_MARKET_TYPE.keys()
        for mt in supported:
            series.extend(DEFAULT_SERIES_BY_MARKET_TYPE.get(mt, []))
        # dedoublonnage en preservant l'ordre
        return list(dict.fromkeys(series))

    # ── crawl complet, EN CACHE (>= 30 min) + incremental ──────────────────
    def _load_disk_cache(self):
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                d = json.load(f)
            self._universe = d.get("markets") or None
            self._universe_ts = float(d.get("fetched_at") or 0)
        except (OSError, ValueError):
            pass

    def _save_disk_cache(self):
        try:
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": self._universe_ts,
                           "scanner_version": SCANNER_VERSION,
                           "markets": self._universe}, f)
            os.replace(tmp, self._cache_path)
        except OSError as e:
            log.warning(f"cache univers non persiste: {e}")

    def _crawl_universe(self) -> list:
        """Pagination complete /markets (cursor). Appelee au maximum une
        fois par universe_ttl_s. L'alerte MAX_PAGES est emise UNE fois par
        rafraichissement si le plafond est atteint."""
        out, cursor, pages = [], None, 0
        while pages < self.cfg.crawl_max_pages:
            params = {"status": "open", "limit": self.cfg.page_limit}
            if cursor:
                params["cursor"] = cursor
            try:
                r = self.client._req("GET", "/markets", params=params)
            except Exception as e:               # noqa: BLE001
                log.error(f"crawl page {pages}: {e}")
                break
            pages += 1
            out.extend(r.get("markets", []) or [])
            cursor = r.get("cursor")
            if not cursor:
                break
        self._crawl_pages_last = pages
        if pages >= self.cfg.crawl_max_pages:
            log.warning(f"Garde-fou MAX_PAGES={self.cfg.crawl_max_pages} "
                        f"atteint lors du RAFRAICHISSEMENT du cache "
                        f"(prochain rafraichissement dans "
                        f">={self.cfg.universe_ttl_s}s) — univers possiblement "
                        f"incomplet ; le perimetre CIBLE n'est pas affecte.")
        return out

    def universe(self, force: bool = False) -> list:
        now = self.now_fn()
        if (not force and self._universe is not None
                and now - self._universe_ts < self.cfg.universe_ttl_s):
            return self._universe
        self._universe = self._crawl_universe()
        self._universe_ts = now
        self._save_disk_cache()
        return self._universe

    def _incremental_refresh(self, base: list) -> list:
        """Entre deux crawls complets : seules les series CIBLES sont
        re-lues (fraiches), fusionnees dans l'univers en cache."""
        fresh = {}
        for s in self.target_series():
            for m in (self.client.get_markets(s, status="open",
                                              limit=self.cfg.page_limit) or []):
                tk = m.get("ticker")
                if tk:
                    fresh[tk] = m
        merged = {m.get("ticker"): m for m in (base or []) if m.get("ticker")}
        merged.update(fresh)
        return list(merged.values())

    # ── cycle de scan ──────────────────────────────────────────────────────
    def scan_cycle(self) -> dict:
        """Retourne {"markets": [...], "report": {...}} avec metriques
        avant/apres chaque filtre."""
        t0 = time.time()
        now_dt = datetime.now(timezone.utc)
        rep = {"scanner_version": SCANNER_VERSION,
               "mode": None, "from_cache": False,
               "crawl_pages_last_refresh": self._crawl_pages_last,
               "excluded_by_reason": {}, "funnel": {}}

        if self.cfg.general_crawl:
            cached = (self._universe is not None and
                      self.now_fn() - self._universe_ts
                      < self.cfg.universe_ttl_s)
            base = self.universe()
            rep["mode"] = "crawl+incremental"
            rep["from_cache"] = cached
            markets = self._incremental_refresh(base) if cached else base
        else:
            rep["mode"] = "targeted_series"
            markets = self._incremental_refresh([])

        def _excl(reason):
            rep["excluded_by_reason"][reason] = \
                rep["excluded_by_reason"].get(reason, 0) + 1

        rep["funnel"]["scanned_raw"] = len(markets)

        kept = []
        for m in markets:
            status = str(m.get("status") or "").lower()
            if status and status not in ("open", "active"):
                _excl("closed_or_settled"); continue
            kept.append(m)
        rep["funnel"]["after_status"] = len(kept)

        stage = []
        for m in kept:
            mins = _minutes_to_close(m, now_dt)
            if mins is None:
                _excl("no_close_time"); continue
            if mins <= 0:
                _excl("expired"); continue
            if mins < self.cfg.min_minutes:
                _excl("closes_too_soon"); continue
            if mins > self.cfg.lookahead_hours * 60:
                _excl("outside_time_window"); continue
            m["_minutes_remaining"] = mins
            stage.append(m)
        kept = stage
        rep["funnel"]["after_time_window"] = len(kept)

        stage = []
        for m in kept:
            if not _has_liquidity(m):
                _excl("no_liquidity"); continue
            stage.append(m)
        kept = stage
        rep["funnel"]["after_liquidity"] = len(kept)

        stage = []
        for m in kept:
            c = classify(m)
            m["_classification"] = c.to_dict()
            if c.market_type == "unknown":
                _excl("unknown_market_type"); continue
            if self.router is not None:
                sup = set(self.router.supported_market_types())
                trd = set(self.router.tradeable_market_types()
                          if hasattr(self.router, "tradeable_market_types")
                          else sup)
                if c.market_type not in sup:
                    _excl("no_compatible_strategy"); continue
                if c.market_type not in trd:
                    _excl("no_probability_provider"); continue
            stage.append(m)
        kept = stage
        rep["funnel"]["after_classification"] = len(kept)

        # plafond d'evaluation couteuse : tri par (liquidite approx, temps)
        def _cheap_score(m):
            vol = 0.0
            for k in ("volume_24h", "volume", "open_interest"):
                try:
                    vol = max(vol, float(m.get(k) or 0))
                except (TypeError, ValueError):
                    pass
            return (vol, -m.get("_minutes_remaining", 1e9))
        kept.sort(key=_cheap_score, reverse=True)
        if len(kept) > self.cfg.max_markets_per_cycle:
            for _ in kept[self.cfg.max_markets_per_cycle:]:
                _excl("over_cycle_cap")
            kept = kept[:self.cfg.max_markets_per_cycle]
        rep["funnel"]["kept"] = len(kept)
        rep["scan_duration_ms"] = int((time.time() - t0) * 1000)
        return {"markets": kept, "report": rep}


# ── Compat --scan-only ──────────────────────────────────────────────────────

def run_scan(client, router=None) -> dict:
    sc = MarketScanner(client, router=router)
    res = sc.scan_cycle()
    rep = res["report"]
    out = {"report": {
        "total_markets_received": rep["funnel"].get("scanned_raw", 0),
        "api_pages": rep.get("crawl_pages_last_refresh", 0),
        "valid_markets": rep["funnel"].get("kept", 0),
        "excluded_by_reason": rep["excluded_by_reason"],
        "funnel": rep["funnel"], "mode": rep["mode"],
    }, "markets": res["markets"]}
    try:
        with open("market_scanner_report.json", "w", encoding="utf-8") as f:
            json.dump(out["report"], f, indent=1)
    except OSError:
        pass
    return out
