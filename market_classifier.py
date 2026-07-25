"""
market_classifier.py — v1 (2026-07-24)
Classification DETERMINISTE des marches Kalshi.

REGLES DE CONCEPTION (correctifs des bugs observes dans les logs) :
  1. Le market_type est derive du PREFIXE de serie du ticker (segment avant
     le premier '-'), jamais d'une recherche de sous-chaine dans le titre.
     Bug corrige : "Las VeGAS" contenait "gas" -> Commodities,
     "GOLDen State" contenait "gold" -> Commodities,
     "KXMEDNOM..." contenait "NOM" -> election_winner.
  2. Si le market_type est un type sportif, la categorie est FORCEE a
     "Sports", quel que soit le titre ou la categorie API.
  3. BTC/ETH/XRP/SOL/DOGE -> Crypto, toujours.
  4. market_type inconnu => classification_confidence=0 ; le ranker ne doit
     JAMAIS donner un score eleve a un marche unknown.
  5. Aucune dependance au reseau ; entree = dict marche brut de l'API.

Champs utilises, par ordre de priorite : ticker, event_ticker, puis (repli
uniquement, avec regex a FRONTIERES DE MOTS) title/subtitle, puis category
API (normalisee).
"""

import re
from dataclasses import dataclass, asdict

CLASSIFIER_VERSION = "clf-v1-2026-07-24"

SPORTS_TYPES = ("sports_moneyline", "sports_spread", "sports_total",
                "sports_other")

# Ligues reconnues (les plus longues d'abord pour eviter NBA de matcher WNBA)
_LEAGUES = ("NASCAR", "WNBA", "ECULP", "NCAAF", "NCAAB", "UEFA", "NBA",
            "NFL", "MLB", "NHL", "CFL", "MLS", "EPL", "UCL", "UFC", "ATP",
            "WTA", "ITF", "PGA", "F1")

# Prefixes de serie EXACTS -> (market_type, category, family)
# Testes contre les tickers reels des logs (voir tests/test_classifier.py).
_PREFIX_RULES = (
    ("KXBTC15M",        ("btc_15m_above_strike",    "Crypto",    "BTC")),
    ("KXBTCD",          ("btc_above_strike_daily",  "Crypto",    "BTC")),
    ("KXBTC",           ("btc_other",               "Crypto",    "BTC")),
    ("KXETH15M",        ("eth_15m_above_strike",    "Crypto",    "ETH")),
    ("KXETHD",          ("eth_above_strike_daily",  "Crypto",    "ETH")),
    ("KXETH",           ("eth_other",               "Crypto",    "ETH")),
    ("KXXRP",           ("crypto_other",            "Crypto",    "XRP")),
    ("KXSOL",           ("crypto_other",            "Crypto",    "SOL")),
    ("KXDOGE",          ("crypto_other",            "Crypto",    "DOGE")),
    ("KXECONSTATCPI",   ("cpi_above_threshold",     "Economics", "CPI")),
    ("KXCPI",           ("cpi_above_threshold",     "Economics", "CPI")),
    ("KXECONSTAT",      ("econ_stat",               "Economics", "ECON")),
    ("KXFED",           ("fed_decision",            "Economics", "FED")),
    ("KXAAAGAS",        ("gas_price_threshold",     "Commodities", "GAS")),
    # election_winner : UNIQUEMENT des series "qui gagne X" explicites.
    # KXELECTIONBILL (adoption d'une loi) et KXMEDNOM (nomination media)
    # ne sont PAS des election_winner (regression corrigee).
    ("KXPRES",          ("election_winner",         "Politics",  "ELECTION")),
    ("KXSENATE",        ("election_winner",         "Politics",  "ELECTION")),
    ("KXGUB",           ("election_winner",         "Politics",  "ELECTION")),
    ("KXMAYOR",         ("election_winner",         "Politics",  "ELECTION")),
    ("KXELECTIONBILL",  ("politics_other",          "Politics",  "POLITICS")),
    ("KXELECTION",      ("election_winner",         "Politics",  "ELECTION")),
)

_WINNER_WORDS = re.compile(r"\b(win(s|ner)?|elected|next president|"
                           r"next mayor|next governor)\b", re.I)


@dataclass
class Classification:
    market_type: str          # ex: sports_spread, btc_above_strike_daily
    category: str             # Sports, Crypto, Politics, Economics, ...
    family: str               # WNBA, MLB, BTC, ELECTION, ...
    confidence: int           # 0 (unknown) .. 3 (prefixe exact)
    source: str               # "prefix" | "league" | "api_category" | "none"

    def to_dict(self):
        return asdict(self)


def series_root(ticker: str) -> str:
    """Segment de serie du ticker : 'KXWNBASPREAD-26JUL20LVTOR-LV7'
    -> 'KXWNBASPREAD'. Deterministe, insensible a la casse."""
    if not ticker:
        return ""
    return str(ticker).split("-", 1)[0].strip().upper()


def _sports_subtype(rest: str) -> str:
    """Sous-type sportif depuis le reste du prefixe apres la ligue.
    L'ordre importe : SPREAD et TOTAL avant GAME/MATCH."""
    if "SPREAD" in rest:
        return "sports_spread"
    if "TOTAL" in rest:
        return "sports_total"
    if "GAME" in rest or "MATCH" in rest or "RACE" in rest \
            or "FIGHT" in rest or rest == "":
        return "sports_moneyline"
    return "sports_other"


def classify(market: dict) -> Classification:
    """Classification deterministe d'un marche. N'utilise QUE des champs de
    l'objet marche ; aucun appel externe, aucun aleatoire."""
    m = market or {}
    tk = str(m.get("ticker") or "")
    ev = str(m.get("event_ticker") or "")
    root = series_root(tk) or series_root(ev)

    # 1) Prefixes exacts (les plus longs d'abord — la table est deja triee
    #    par specificite pour chaque famille).
    for prefix, (mtype, cat, fam) in _PREFIX_RULES:
        if root.startswith(prefix):
            return Classification(mtype, cat, fam, 3, "prefix")

    # 2) Ligues sportives : le token de ligue doit etre AU DEBUT du root
    #    (apres 'KX'). Jamais de sous-chaine libre : 'KXAAAGASW' ne matche
    #    aucune ligue, 'GAME' seul ne suffit pas.
    body = root[2:] if root.startswith("KX") else root
    for lg in _LEAGUES:
        if body.startswith(lg):
            sub = _sports_subtype(body[len(lg):])
            return Classification(sub, "Sports", lg, 3, "league")

    # 3) Repli titre : regex a frontieres de mots, categories larges
    #    seulement (jamais de market_type invente depuis un titre).
    title = f"{m.get('title') or ''} {m.get('subtitle') or ''}"
    api_cat = str(m.get("category") or "").strip().title()
    if api_cat in ("Politics", "Elections") and _WINNER_WORDS.search(title):
        return Classification("election_winner", "Politics", "ELECTION",
                              2, "api_category")

    # 4) Categorie API presente : market_type GROSSIER deterministe
    #    (identifiable != tradable ; ces types ne sont pas au registre et
    #    sont rejetes no_compatible_strategy — plus jamais "unknown" pour
    #    un marche identifiable, cf. Tache 5).
    _COARSE = {"Sports": "sports_other", "Crypto": "crypto_other",
               "Economics": "econ_stat", "Financials": "financials_other",
               "Commodities": "commodities_other",
               "Climate And Weather": "weather_other",
               "Weather": "weather_other", "Politics": "politics_other",
               "Elections": "politics_other", "World": "world_other",
               "Entertainment": "entertainment_other",
               "Tech & Science": "tech_other",
               "Science And Technology": "tech_other"}
    if api_cat in _COARSE:
        return Classification(_COARSE[api_cat], api_cat, "COARSE", 1,
                              "api_category")
    if api_cat:
        return Classification("unknown", api_cat, "UNKNOWN", 0,
                              "api_category")
    return Classification("unknown", "Other", "UNKNOWN", 0, "none")
