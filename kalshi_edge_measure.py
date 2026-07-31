"""
kalshi_edge_measure.py  --  Mesure d'edge SANS trader

But : repondre a UNE seule question, honnetement et chiffres a l'appui :
"Est-ce que ma regle de decision a un avantage reel, NET DE FRAIS,
 par rapport a simplement payer le prix du marche ?"

Aucun ordre reel n'est passe. On enregistre ce que la regle AURAIT fait,
on recupere le resultat officiel de Kalshi apres cloture, puis on calcule.

3 etapes :
  1) record  -> a chaque cycle, appeler record_snapshot(...) depuis ton bot
  2) resolve -> apres cloture, joindre le resultat reel (result yes/no)
  3) analyze -> calculer taux de reussite, PnL net, et surtout la CALIBRATION

Le test qui compte (calibration) :
  Si tu achetes a un prix moyen de 0.62 et que tu gagnes 62 % du temps,
  ton edge BRUT est nul -> et NEGATIF apres frais. Un edge n'existe que si
  ton taux de reussite reel depasse nettement le prix que tu paies.

IMPORTANT / a verifier toi-meme (je ne peux pas le garantir a l'aveugle) :
  - Le champ de resultat de l'API Kalshi : ce module lit market["result"]
    ("yes"/"no") et market["status"]. Verifie ces noms dans la doc Kalshi
    a jour ou en imprimant un marche regle : print(kalshi.get_market(ticker)).
  - Le calcul de frais ci-dessous est une APPROXIMATION du bareme Kalshi
    (formule ~ 0.07 * C * P * (1-P)). Verifie le bareme en vigueur et ajuste
    --fee-coef si besoin.
  - En demo, on suppose un fill au prix affiche : la realite a du slippage
    et des fills partiels. Les resultats demo sont donc OPTIMISTES.
"""

import json
import math
import os
import time
import argparse
from datetime import datetime, timezone

SNAP_FILE = "edge_snapshots.jsonl"   # une ligne JSON par cycle enregistre


# ────────────────────────────────────────────────────────────────────────────
# ETAPE 1 : ENREGISTREMENT  (a appeler depuis ton bot, sans passer d'ordre)
# ────────────────────────────────────────────────────────────────────────────

def record_snapshot(market_data: dict, decision: dict,
                    coinbase_spot: float = None, path: str = SNAP_FILE) -> None:
    """
    A appeler une fois par cycle BTC, APRES avoir calcule la decision et
    AVANT (ou a la place) d'executer. N'ecrit qu'une ligne, ne trade pas.

    market_data : le dict du marche Kalshi (celui que ton bot utilise deja)
    decision    : le dict retourne par make_btc_decision (verdict, yes_cents...)
    coinbase_spot : get_btc_price() (facultatif, sert a mesurer l'ecart source)
    """
    ticker = market_data.get("ticker")
    if not ticker:
        return

    row = {
        "recorded_at":  time.time(),
        "ticker":       ticker,
        "close_time":   market_data.get("close_time"),
        "strike":       (market_data.get("floor_strike")
                         or market_data.get("strike_price")),
        "yes_bid":      _as_int(market_data.get("yes_bid")),
        "no_bid":       _as_int(market_data.get("no_bid")),
        "verdict":      decision.get("verdict", "AUCUN TRADE"),
        "confiance":    decision.get("confiance", 0),
        "coinbase_spot": coinbase_spot,
        # champs de resultat, remplis plus tard par resolve()
        "resolved":     False,
        "result":       None,     # "yes" / "no"
        "won":          None,     # True / False (selon le verdict)
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────────
# ETAPE 2 : RESOLUTION  (joindre le resultat reel apres cloture)
# ────────────────────────────────────────────────────────────────────────────

def resolve_pending(fetch_market_fn, path: str = SNAP_FILE) -> dict:
    """
    Pour chaque snapshot non resolu dont la fenetre est close, recupere le
    marche via fetch_market_fn(ticker) et lit le resultat officiel.

    fetch_market_fn : une fonction ticker -> dict, par ex. kalshi.get_market
                      (on la passe en argument pour ne rien supposer de ton
                       client). LECTURE SEULE, aucun ordre.

    Retourne un petit resume {checked, newly_resolved, still_pending}.
    """
    rows = _load_rows(path)
    if not rows:
        return {"checked": 0, "newly_resolved": 0, "still_pending": 0}

    now = time.time()
    cache = {}          # ticker -> result deja recupere ce run
    checked = newly = pending = 0

    for row in rows:
        if row.get("resolved"):
            continue
        ct = _parse_close_epoch(row.get("close_time"))
        # on n'interroge que les fenetres deja closes (marge 90s)
        if ct is None or now < ct + 90:
            pending += 1
            continue

        ticker = row["ticker"]
        checked += 1
        if ticker not in cache:
            try:
                m = fetch_market_fn(ticker) or {}
            except Exception:
                m = {}
            cache[ticker] = _extract_result(m)
        result = cache[ticker]

        if result in ("yes", "no"):
            row["resolved"] = True
            row["result"] = result
            row["won"] = _did_win(row.get("verdict", ""), result)
            newly += 1
        else:
            pending += 1

    _save_rows(rows, path)
    return {"checked": checked, "newly_resolved": newly, "still_pending": pending}


def _extract_result(market: dict):
    """
    Lit le resultat regle. VERIFIE ces noms de champ contre l'API Kalshi :
    en general market["result"] vaut "yes"/"no"/"" et market["status"] passe
    a "settled"/"finalized". On reste defensif.
    """
    for key in ("result", "settlement_result", "outcome"):
        v = market.get(key)
        if isinstance(v, str) and v.lower() in ("yes", "no"):
            return v.lower()
    return None


def _did_win(verdict: str, result: str) -> bool:
    v = verdict.upper()
    if "YES" in v:
        return result == "yes"
    if "NO" in v:
        return result == "no"
    return False   # AUCUN TRADE -> pas de position


# ────────────────────────────────────────────────────────────────────────────
# ETAPE 3 : ANALYSE  (100 % hors-ligne, aucun reseau)
# ────────────────────────────────────────────────────────────────────────────

def analyze(path: str = SNAP_FILE, fill: str = "ask",
            fee_coef: float = 0.07) -> None:
    rows = [r for r in _load_rows(path) if r.get("resolved")]
    trades = [r for r in rows
              if r.get("verdict", "AUCUN TRADE") != "AUCUN TRADE"
              and r.get("won") is not None]

    print("=" * 62)
    print("  MESURE D'EDGE  (demo, hors-ligne)")
    print("=" * 62)
    print(f"Snapshots resolus         : {len(rows)}")
    print(f"Trades declenches (regle) : {len(trades)}")
    if not trades:
        print("\nPas encore de trade resolu. Laisse tourner en --demo,")
        print("puis lance 'resolve' avant de relancer 'analyze'.")
        return

    # Prix d'entree selon l'hypothese de fill
    entries, wins, pnls = [], [], []
    for r in trades:
        p = _entry_price(r, fill)          # en dollars 0..1
        if p is None:
            continue
        won = bool(r["won"])
        fee = _kalshi_fee(p, fee_coef)
        pnl = ((1.0 - p) if won else (-p)) - fee
        entries.append(p); wins.append(1 if won else 0); pnls.append(pnl)

    n = len(entries)
    if n == 0:
        print("Prix d'entree indisponibles (yes_bid/no_bid manquants).")
        return

    w   = sum(wins) / n                    # taux de reussite reel
    p_  = sum(entries) / n                 # prix moyen paye
    net = sum(pnls)                        # PnL net total (par 1 contrat/trade)
    per = net / n                          # PnL net moyen par trade

    # Intervalle de confiance (Wald 95 %) sur le taux de reussite
    se  = math.sqrt(max(w * (1 - w), 1e-12) / n)
    lo, hi = w - 1.96 * se, w + 1.96 * se

    edge_brut = w - p_                     # esperance brute par contrat
    fee_moy   = sum(_kalshi_fee(p, fee_coef) for p in entries) / n

    print(f"Hypothese de fill         : {fill}  (ask = realiste)")
    print(f"Frais approx / contrat    : ~{fee_moy*100:.2f} c   (coef={fee_coef})")
    print("-" * 62)
    print(f"Taux de reussite reel     : {w*100:.1f} %   (IC95 {lo*100:.1f}-{hi*100:.1f} %)")
    print(f"Prix moyen paye           : {p_*100:.1f} c")
    print(f"Edge BRUT (reussite-prix) : {edge_brut*100:+.1f} c / contrat")
    print(f"PnL NET moyen / trade     : {per*100:+.2f} c   (total {net*100:+.1f} c sur {n})")
    print("=" * 62)

    # Verdict de calibration, avec honnetete sur la taille d'echantillon
    print("VERDICT :")
    if n < 100:
        print(f"  Echantillon trop petit (n={n}). RIEN n'est concluant en dessous")
        print("  de ~100-300 trades resolus. Continue a collecter avant de juger.")
    if lo > p_:
        print("  Le bas de l'IC95 du taux de reussite depasse le prix paye :")
        print("  signe d'un edge POSSIBLE. A confirmer sur plus de donnees ET en")
        print("  conditions reelles (les fills demo sont optimistes).")
    elif hi < p_:
        print("  Le taux de reussite est significativement SOUS le prix paye :")
        print("  la regle perd meme avant frais. Pas d'edge.")
    else:
        print("  Taux de reussite ~ prix paye : marche efficient, edge brut ~0.")
        print("  Apres frais, l'esperance est negative. C'est le cas le plus")
        print("  frequent pour une regle qui 'suit le prix'.")
    if per < 0:
        print(f"  PnL net moyen NEGATIF ({per*100:+.2f} c/trade) : cette regle perd de")
        print("  l'argent nette de frais sur l'echantillon actuel.")
    print("\nRappel : demo != reel (pas de slippage/fills partiels modelises),")
    print("et je ne suis pas conseiller financier. Ceci mesure, ne recommande pas.")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _entry_price(row: dict, fill: str):
    """Prix paye en dollars (0..1) pour le cote achete, selon l'hypothese."""
    yes_b, no_b = row.get("yes_bid"), row.get("no_bid")
    if yes_b is None or no_b is None:
        return None
    yes_ask = 100 - no_b     # acheter YES coute ~ 100 - no_bid
    no_ask  = 100 - yes_b    # acheter NO  coute ~ 100 - yes_bid
    v = row.get("verdict", "").upper()
    if "YES" in v:
        bid, ask, mid = yes_b, yes_ask, (yes_b + yes_ask) / 2
    elif "NO" in v:
        bid, ask, mid = no_b, no_ask, (no_b + no_ask) / 2
    else:
        return None
    cents = {"bid": bid, "ask": ask, "mid": mid}.get(fill, ask)
    return max(1, min(99, cents)) / 100.0


def _kalshi_fee(price_dollars: float, coef: float) -> float:
    """
    Approximation du bareme Kalshi pour 1 contrat, en dollars.
    Formule courante : ceil(coef * C * P * (1-P)) arrondie au cent.
    A VERIFIER : le bareme exact varie et a pu changer.
    """
    raw = coef * 1 * price_dollars * (1 - price_dollars)
    return math.ceil(raw * 100) / 100.0


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_close_epoch(ct):
    if not ct:
        return None
    try:
        return datetime.fromisoformat(str(ct).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _load_rows(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _save_rows(rows, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, path)


# ── CLI ───────────────────────────────────────────────────────────────────

def _main():
    ap = argparse.ArgumentParser(description="Mesure d'edge Kalshi (sans trader)")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("analyze", help="Calculer l'edge (hors-ligne)")
    a.add_argument("--file", default=SNAP_FILE)
    a.add_argument("--fill", default="ask", choices=["bid", "ask", "mid"])
    a.add_argument("--fee-coef", type=float, default=0.07)

    r = sub.add_parser("resolve", help="Joindre les resultats reels via ton client")
    r.add_argument("--file", default=SNAP_FILE)

    args = ap.parse_args()

    if args.cmd == "analyze":
        analyze(args.file, fill=args.fill, fee_coef=args.fee_coef)
    elif args.cmd == "resolve":
        # On importe TON client en lecture seule. Si l'import declenche des
        # effets de bord, lance plutot resolve depuis ton propre script en
        # appelant resolve_pending(kalshi.get_market).
        try:
            from kalshi_client import KalshiClient
            kc = KalshiClient("prod")   # lecture seule : get_market uniquement (donnees publiques)
            summary = resolve_pending(kc.get_market, args.file)
            print(summary)
        except Exception as e:
            print(f"Impossible d'importer/instancier KalshiClient : {e}")
            print("Solution : dans ton propre script, fais :")
            print("  from kalshi_edge_measure import resolve_pending")
            print("  print(resolve_pending(kalshi.get_market))")
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()
