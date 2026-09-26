"""Public GET-only supplementary evidence. No predictions, credentials or orders.

This is a separate append-only ledger, never a migration of observations.sqlite.
Derived values are reconstructed from receipts before research admission.
Coinbase is a reference venue, NOT the BRTI settlement oracle.
"""
import base64
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, build_opener

from .data import NoRedirect, ORIGIN, MAX_BYTES, MAX_PAGES
from .domain import Refused, canonical, decimal, digest, now, strict_json, utc
from .research_export import export_database, verify_snapshot

COINBASE = "https://api.exchange.coinbase.com"
FEE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
SERIES_URL = ORIGIN + "/trade-api/v2/series/KXBTC15M"
TICKER_URL = COINBASE + "/products/BTC-USD/ticker"
CANDLES_URL = COINBASE + "/products/BTC-USD/candles"
KINDS = {"Q_RAW", "Q_FAILURE", "Q_LABEL", "Q_REFERENCE", "Q_CANDLES", "Q_REFRESH", "Q_LADDER", "Q_FEE"}


def ticker(value):
    if not isinstance(value, str) or not re.fullmatch(r"KXBTC15M-[A-Z0-9-]{1,100}", value):
        raise Refused("outside frozen V2 market scope")
    return value


def permitted(url):
    """Exact public paths and query keys; no arbitrary URL or credential entry."""
    p = urlsplit(url)
    q = parse_qs(p.query, keep_blank_values=True)
    if p.fragment or any(len(v) != 1 for v in q.values()):
        return False
    if url in {TICKER_URL, SERIES_URL, FEE_URL}:
        return True
    if p.scheme == "https" and p.netloc == "api.exchange.coinbase.com" and p.path == "/products/BTC-USD/candles":
        return set(q) == {"start", "end", "granularity"} and q["granularity"] == ["60"]
    if p.scheme != "https" or p.netloc != "external-api.kalshi.com":
        return False
    if re.fullmatch(r"/trade-api/v2/markets/KXBTC15M-[A-Z0-9-]{1,100}", p.path):
        return not q
    return (p.path == "/trade-api/v2/markets" and set(q) <= {"event_ticker", "limit", "cursor"}
            and q.get("limit") == ["200"] and bool(re.fullmatch(r"KXBTC15M-[A-Z0-9-]{1,100}", q.get("event_ticker", [""])[0])))


class Reader:
    def get(self, url):
        if not permitted(url):
            raise Refused("public qualification URL not permitted")
        request = Request(url, method="GET", headers={"Accept-Encoding": "identity", "User-Agent": "Atlas-V2-public-research/1"})
        started = now()
        try:
            response = build_opener(NoRedirect).open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_BYTES+1)
            length = response.headers.get("Content-Length")
            return {"url":url, "response_url":response.geturl(), "method":"GET", "status":response.status,
                    "started_at":started, "received_at":now(), "content_type":response.headers.get("Content-Type", "").split(";")[0].strip().lower(),
                    "content_range":response.headers.get("Content-Range"),
                    "transport_complete":len(raw)<=MAX_BYTES and response.headers.get("Content-Range") is None
                        and (length is None or length.isdigit() and int(length)==len(raw)),
                    "body_base64":base64.b64encode(raw[:MAX_BYTES]).decode(), "body_sha256":hashlib.sha256(raw[:MAX_BYTES]).hexdigest()}


def body(receipt, expected_url):
    p = receipt["payload"]
    if (receipt["kind"] != "Q_RAW" or not permitted(expected_url) or p["url"] != expected_url
            or p["response_url"] != expected_url or p["method"] != "GET" or p["status"] != 200
            or p["transport_complete"] is not True or p.get("content_range") is not None
            or utc(p["received_at"]) < utc(p["started_at"])):
        raise Refused("incomplete/unattributed qualification receipt")
    raw = base64.b64decode(p["body_base64"], validate=True)
    if len(raw)>MAX_BYTES or hashlib.sha256(raw).hexdigest() != p["body_sha256"]:
        raise Refused("qualification receipt body hash")
    if expected_url == FEE_URL:
        if p["content_type"] != "application/pdf" or not raw.startswith(b"%PDF-"):
            raise Refused("fee schedule is not PDF")
        return raw
    if p["content_type"] != "application/json":
        raise Refused("qualification source did not return JSON")
    strict_json(raw)  # duplicate keys, nonfinite values and invalid encoding fail
    return json.loads(raw, parse_float=str)  # retain native decimal precision


def market(receipt, observation):
    data = body(receipt, ORIGIN + "/trade-api/v2/markets/" + ticker(observation["ticker"]))
    if not isinstance(data, dict) or set(data) != {"market"}:
        raise Refused("market envelope")
    m = data["market"]
    if (m["ticker"] != observation["ticker"] or m["event_ticker"] != observation["event_id"]
            or utc(m["close_time"]) != utc(observation["close_at"]) or m["market_type"] != "binary"):
        raise Refused("market identity/expiry binding")
    return m


def settlement(receipt, observation):
    m = market(receipt, observation)
    if m["status"] != "finalized" or m.get("is_provisional", False) is not False or m.get("result") not in {"yes", "no"}:
        raise Refused("not an authoritative final binary settlement")
    settled = utc(m["settlement_ts"])
    if not utc(observation["close_at"]) <= settled <= utc(receipt["payload"]["received_at"]):
        raise Refused("settlement timestamp outside closed/received bounds")
    outcome = int(m["result"] == "yes")
    if decimal(m["settlement_value_dollars"]) != outcome:
        raise Refused("settlement payout contradicts result")
    return {"ticker":m["ticker"], "close_at":observation["close_at"], "outcome":outcome,
            "settlement_at":m["settlement_ts"], "published_at":receipt["payload"]["received_at"],
            "source_receipt":receipt["hash"], "authority":"Kalshi finalized market", "raw_market_hash":digest(m)}


def reference(receipt):
    d = body(receipt, TICKER_URL)
    at, received = utc(d["time"]), utc(receipt["payload"]["received_at"])
    if not 0 <= (received-at).total_seconds() <= 5 or decimal(d["price"]) <= 0 or type(d["trade_id"]) is not int:
        raise Refused("stale/future/malformed BTC reference")
    return {"at":d["time"], "price":str(decimal(d["price"])), "receipt":receipt["hash"],
            "source":"Coinbase Exchange BTC-USD last trade", "trade_id":d["trade_id"],
            "received_at":receipt["payload"]["received_at"], "interpolated":False}


def candle_url(end):
    end = utc(end)
    if end.timestamp() % 60:
        raise Refused("minute boundary required")
    return CANDLES_URL + "?" + urlencode({"start":(end-timedelta(minutes=31)).isoformat(), "end":end.isoformat(), "granularity":60})


def candles(receipt, end):
    d = body(receipt, candle_url(end))
    end_at = utc(end)
    if not isinstance(d, list) or not 31 <= len(d) <= 300 or end_at > utc(receipt["payload"]["started_at"]):
        raise Refused("incomplete/nonclosed candle response")
    indexed = {}
    for c in d:
        if not isinstance(c, list) or len(c) != 6 or type(c[0]) is not int or c[0] % 60 or c[0] in indexed:
            raise Refused("malformed/duplicate candle timing")
        low, high, opened, close, volume = map(decimal, (str(v) for v in c[1:]))
        if not 0 < low <= min(opened, close) <= max(opened, close) <= high or volume < 0:
            raise Refused("malformed candle OHLCV")
        indexed[c[0]] = c
    starts = [int(end_at.timestamp())-60*i for i in range(31,0,-1)]
    if any(t not in indexed for t in starts):
        raise Refused("incomplete candle window; interpolation prohibited")
    rows = [{"opened_at":datetime.fromtimestamp(t,timezone.utc).isoformat(),
             "closed_at":datetime.fromtimestamp(t+60,timezone.utc).isoformat(),
             "close":str(indexed[t][4]), "closed":True, "receipt":receipt["hash"],
             "source":"Coinbase Exchange BTC-USD", "received_at":receipt["payload"]["received_at"]} for t in starts]
    return {"closed_candles":rows,"complete":True,"end":end,
            "extra_raw_candles":len(d)-31,"selection":"exact 31 closed consecutive minute buckets; all raw rows retained"}


def refreshed(receipt, observation):
    m = market(receipt, observation)
    at = receipt["payload"]["received_at"]
    if (m["status"] != "active" or not utc(observation["observed_at"]) <= utc(receipt["payload"]["started_at"])
            or not 0 <= (utc(at)-utc(observation["observed_at"])).total_seconds() <= 5
            or not 0 <= (utc(at)-utc(m["updated_time"])).total_seconds() <= 5 or utc(at) >= utc(m["close_time"])):
        raise Refused("refresh stale/future/closed or not after decision")
    bid, ask, size = map(decimal, (m["yes_bid_dollars"],m["yes_ask_dollars"],m["yes_ask_size_fp"]))
    if not 0 <= bid <= ask < 1 or ask <= 0 or size < 0:
        raise Refused("invalid refreshed price/depth")
    steps = [decimal(r["step"]) for r in m["price_ranges"] if decimal(r["start"]) <= ask <= decimal(r["end"])]
    if not steps or min(steps)<=0:
        raise Refused("price increment unavailable")
    # Ex ante stress assumption, not an observed fill or proven latency bound.
    slip = max(steps) + max(decimal(0), ask-decimal(observation["ask"]))
    return {"ticker":m["ticker"],"side":"yes","bid":str(bid),"ask":str(ask),"available":str(size),
            "observed_at":at,"closes_at":observation["close_at"],"receipt_hash":receipt["hash"],
            "decision_hash":observation["hash"],"decision_price":observation["ask"],"spread":str(ask-bid),
            "slippage_assumption":str(slip),"slippage_policy":"one quoted tick plus positive decision-to-refresh ask move",
            "slippage_qualified":False,"liquidity_kind":"displayed top-of-book, not a fill guarantee"}


def ladder(receipts, observation):
    cursor, seen, rows = "", set(), []
    if not receipts or len(receipts)>MAX_PAGES:
        raise Refused("ladder page bound")
    for i,r in enumerate(receipts):
        q = {"event_ticker":observation["event_id"],"limit":200}
        if cursor: q["cursor"] = cursor
        d = body(r, ORIGIN+"/trade-api/v2/markets?"+urlencode(q))
        if not isinstance(d,dict) or set(d)!={"markets","cursor"} or not isinstance(d["markets"],list) or not isinstance(d["cursor"],str):
            raise Refused("incomplete ladder envelope")
        if not 0 <= (utc(observation["observed_at"])-utc(r["payload"]["received_at"])).total_seconds() <= 5:
            raise Refused("ladder not available before decision")
        for m in d["markets"]:
            if m["ticker"] in seen or m["event_ticker"] != observation["event_id"]:
                raise Refused("duplicate/mismatched ladder market")
            ticker(m["ticker"])
            seen.add(m["ticker"])
            rows.append((m,r))
        cursor = d["cursor"]
        if (not cursor) != (i==len(receipts)-1):
            raise Refused("nonterminal/late ladder page")
    # Full event must comprise economically comparable legs; no dropping bad legs.
    contracts=[]
    for m,r in rows:
        if (m["status"] != "active" or m["market_type"] != "binary" or m["strike_type"] != "greater"
                or utc(m["close_time"]) != utc(observation["close_at"])
                or not 0 <= (utc(observation["observed_at"])-utc(m["updated_time"])).total_seconds() <= 5):
            raise Refused("incomparable/stale ladder leg (including >= versus >)")
        rules = {k:m[k] for k in ("rules_primary","rules_secondary")}
        if not all(isinstance(v,str) and v for v in rules.values()):
            raise Refused("missing contract rules")
        bid,ask=decimal(m["yes_bid_dollars"]),decimal(m["yes_ask_dollars"])
        if not 0 <= bid <= ask <= 1 or decimal(m["yes_ask_size_fp"]) < 0:
            raise Refused("ladder quote/depth")
        contracts.append({"ticker":m["ticker"],"bid":str(bid),"ask":str(ask),"available":m["yes_ask_size_fp"],
                          "strike":str(decimal(str(m["floor_strike"]))),"close_at":m["close_time"],"direction":"greater_than",
                          "observed_at":r["payload"]["received_at"],"rules_receipt":digest(rules),
                          "reference":rules["rules_secondary"],"source_receipt":r["hash"]})
    contracts.sort(key=lambda c:decimal(c["strike"]))
    centers=[i for i,c in enumerate(contracts) if c["ticker"]==observation["ticker"]]
    if len(centers)!=1 or centers[0]==0 or centers[0]==len(contracts)-1:
        raise Refused("no same-expiry neighboring strikes in frozen scope")
    triple=contracts[centers[0]-1:centers[0]+2]
    if any(c["rules_receipt"]!=triple[1]["rules_receipt"] for c in triple):
        raise Refused("neighbor rules not identical; semantic equivalence unproved")
    return {"strike_triple":triple,"pages":[r["hash"] for r in receipts],"terminal_cursor":"","complete":True}


def fee_evidence(series_receipt, schedule_receipt, price):
    d=body(series_receipt,SERIES_URL)
    pdf=body(schedule_receipt,FEE_URL)
    if set(d)!={"series"} or d["series"]["ticker"]!="KXBTC15M" or d["series"]["fee_type"]!="quadratic":
        raise Refused("unrecognized series fee model")
    m=decimal(str(d["series"]["fee_multiplier"]))
    p=decimal(price)
    if m<=0 or not 0<p<1: raise Refused("fee multiplier/price missing or zero")
    bound=(decimal("0.07")*m*p*(1-p)+decimal("0.01")).quantize(decimal("0.01"),rounding=ROUND_CEILING)
    return {"fee_bound":str(bound),"multiplier":str(m),"series_receipt":series_receipt["hash"],
            "schedule_receipt":schedule_receipt["hash"],"schedule_sha256":hashlib.sha256(pdf).hexdigest(),
            "rule":"0.07*M*p*(1-p) plus 1 cent rounding allowance, rounded up to cents; one contract",
            "qualification":"UNVERIFIED_SCHEDULE_VERSION_AND_ACCOUNT_FEE_CLASS",
            "fee_qualified":False}


class Collector:
    def __init__(self, store, reader=None):
        self.store,self.reader=store,reader or Reader()

    def raw(self,url):
        p=self.reader.get(url)
        return self.store.append("q-raw:"+digest(p),"Q_RAW",p)

    def attempt(self,kind,identity,fetch,derive):
        try:
            raw=fetch()
            value=derive(raw)
            return self.store.append(kind+":"+identity+":"+digest(value),kind,value)
        except Exception as exc:
            value={"operation":kind,"identity":identity,"reason":type(exc).__name__+":"+str(exc)[:240]}
            self.store.append("q-failure:"+now()+":"+digest(value),"Q_FAILURE",value)
            return None

    def reference(self):
        return self.attempt("Q_REFERENCE","BTC-USD",lambda:self.raw(TICKER_URL),reference)

    def candles(self):
        end=utc(now()).replace(second=0,microsecond=0).isoformat()
        return self.attempt("Q_CANDLES",end,lambda:self.raw(candle_url(end)),lambda r:candles(r,end))

    def settlement(self,obs):
        return self.attempt("Q_LABEL",obs["ticker"],lambda:self.raw(ORIGIN+"/trade-api/v2/markets/"+ticker(obs["ticker"])),lambda r:settlement(r,obs))

    def refresh(self,obs):
        return self.attempt("Q_REFRESH",obs["hash"],lambda:self.raw(ORIGIN+"/trade-api/v2/markets/"+ticker(obs["ticker"])),lambda r:refreshed(r,obs))

    def ladder_pages(self,event_id):
        ticker(event_id)
        pages,cursor,cursors=[],"",set()
        for _ in range(MAX_PAGES):
            q={"event_ticker":event_id,"limit":200}
            if cursor:q["cursor"]=cursor
            r=self.raw(ORIGIN+"/trade-api/v2/markets?"+urlencode(q)); pages.append(r)
            d=body(r,r["payload"]["url"])
            if set(d)!={"markets","cursor"} or not isinstance(d["cursor"],str):raise Refused("ladder envelope")
            cursor=d["cursor"]
            if not cursor:return pages
            if cursor in cursors:raise Refused("repeated ladder cursor")
            cursors.add(cursor)
        raise Refused("ladder page bound reached")

    def fees(self,obs,receipts):
        return self.attempt("Q_FEE",obs["hash"],lambda:receipts,
                            lambda r:{"decision_hash":obs["hash"],"price":obs["ask"],**fee_evidence(*r,obs["ask"])})


def export_qualification(database,directory):
    snapshot=export_database(database)
    for e in snapshot["events"]:
        if e["kind"] not in KINDS or e["kind"]=="Q_RAW" and not permitted(e["payload"]["url"]):
            raise Refused("qualification export contains non-public scope")
    destination=Path(directory)/digest(snapshot)
    destination.mkdir(parents=True,exist_ok=True)
    raw=canonical(snapshot)
    # Reuse bounded, deterministic text chunking; original market export untouched.
    import gzip
    encoded=base64.b64encode(gzip.compress(raw,mtime=0))
    parts=[encoded[i:i+60000] for i in range(0,len(encoded),60000)]
    manifest={"schema":"atlas-v2-qualification-export/1","dataset_sha256":digest(snapshot),"anchor":snapshot["anchor"],
              "event_count":len(snapshot["events"]),"uncompressed_bytes":len(raw),"filtering":"NONE",
              "chunks":[{"name":f"part-{i:04d}.txt","bytes":len(b),"sha256":hashlib.sha256(b).hexdigest()} for i,b in enumerate(parts)]}
    for name,b in [(p["name"],b) for p,b in zip(manifest["chunks"],parts)]+[("manifest.json",canonical(manifest))]:
        path=destination/name
        if path.exists():
            if path.read_bytes()!=b:raise Refused("qualification export collision")
        else:
            with path.open("xb") as f:f.write(b)
    return {"manifest_path":str(destination/"manifest.json"),"manifest_sha256":digest(manifest),**manifest}


def verified_events(snapshot,anchor):
    verify_snapshot(snapshot,anchor)
    if snapshot["anchor"]!=anchor:raise Refused("qualification final external anchor required")
    if any(e["kind"] not in KINDS for e in snapshot["events"]):raise Refused("qualification event scope")
    if any(e["kind"]=="Q_RAW" and (not permitted(e["payload"]["url"]) or e["payload"]["method"]!="GET") for e in snapshot["events"]):
        raise Refused("qualification raw source scope")
    return {e["hash"]:e for e in snapshot["events"]}


def prepare(collector, observations):
    """Pre-decision sources only; no re-timestamping or retrospective fills."""
    latest={o["ticker"]:o for o in observations}
    candidates=[o for o in latest.values() if 240 <= (utc(o["close_at"])-utc(now())).total_seconds() <= 300]
    pages={}
    if candidates:
        try:pages["_fees"]=(collector.raw(SERIES_URL),collector.raw(FEE_URL))
        except Exception as exc:
            collector.store.append("q-fee-failure:"+now(),"Q_FAILURE",{"operation":"Q_FEE","identity":"KXBTC15M","reason":str(exc)[:240]})
        collector.candles()
    for o in candidates[:4]:
        try:pages[o["event_id"]]=collector.ladder_pages(o["event_id"])
        except Exception as exc:
            collector.store.append("q-ladder-failure:"+now(),"Q_FAILURE",{"operation":"Q_LADDER","identity":o["event_id"],"reason":str(exc)[:240]})
    collector.reference()
    return pages


def complete_decisions(collector, observations, pages):
    from .alpha_lab import cohort
    rows,_=cohort(observations)
    done={e["payload"]["decision_hash"] for e in collector.store.events("Q_REFRESH")}
    for row in rows[-4:]:
        o=row["observation"]
        if o["hash"] in done or not 0 <= (utc(now())-utc(o["observed_at"])).total_seconds() <= 5:continue
        collector.refresh(o)
        collector.attempt("Q_LADDER",o["hash"],lambda:pages.get(o["event_id"],[]),
                          lambda r:{"decision_hash":o["hash"],**ladder(r,o)})
        collector.fees(o,pages.get("_fees",()))


def pending_settlements(collector, observations):
    """Eight oldest-due markets per minute; final labels rechecked daily.

    Revisions append and are surfaced as conflicts during admission, never repair
    an earlier label in place. Failed attempts have a retry timestamp too.
    """
    markets={o["ticker"]:o for o in observations if utc(o["close_at"]) <= utc(now())}
    last={}
    final=set()
    for e in collector.store.events():
        p=e["payload"]
        if e["kind"]=="Q_LABEL":
            final.add(p["ticker"]); last[p["ticker"]]=e["recorded_at"]
        elif e["kind"]=="Q_FAILURE" and p.get("operation")=="Q_LABEL":
            last[p["identity"]]=e["recorded_at"]
    due=[o for t,o in markets.items() if t not in final or t not in last or (utc(now())-utc(last[t])).total_seconds() >= 86400]
    for o in sorted(due,key=lambda o:(last.get(o["ticker"],""),o["ticker"]))[:8]:collector.settlement(o)


def main():
    """Bounded workstation backfill. Original observation snapshot is read only."""
    import argparse
    from .research_export import observations_from_snapshot
    from .store import Store
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("snapshot");p.add_argument("anchor");p.add_argument("database")
    p.add_argument("--max-markets",type=int,default=8)
    a=p.parse_args()
    if not 1<=a.max_markets<=8:raise Refused("bounded backfill requires 1..8 markets")
    if Path(a.database).exists():raise Refused("use a new explicit supplementary database; no overwrite")
    rows=observations_from_snapshot(strict_json(Path(a.snapshot).read_bytes()),strict_json(Path(a.anchor).read_bytes()))
    s=Store(a.database);c=Collector(s)
    unique={r["ticker"]:r for r in rows if utc(r["close_at"])<=utc(now())}
    for t in sorted(unique)[:a.max_markets]:c.settlement(unique[t])
    c.reference();c.candles()
    print(json.dumps({"anchor":s.anchor(),"labels":len(s.events("Q_LABEL")),"raw_receipts":len(s.events("Q_RAW")),
                      "failures":len(s.events("Q_FAILURE")),"financial_mutations":0}))
    s.db.close()


if __name__=="__main__":main()
