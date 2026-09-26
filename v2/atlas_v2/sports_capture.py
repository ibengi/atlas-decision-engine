"""Bounded prospective public sports receipts; never trading or approval.

REST books do not document quote-as-of timestamps. They are retained but cannot
pass the frozen source-time gate. A source-attested streaming adapter is still
required; this module never invents timestamps or accepts caller probabilities.
No service hook, credential reader, authenticated transport, or historical path.
"""
import argparse
import base64
from datetime import timedelta
import hashlib
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
import uuid

from .data import NoRedirect, ORIGIN, MAX_BYTES
from .domain import Refused, canonical, decimal, digest, now, strict_json, utc
from .store import Store

SYNC_MS = 250
AGE_MS = 1000
MAX_REQUESTS = 100
MAX_PAGES = 20
MAX_RUNTIME_SECONDS = 120
PATHS = {"/search/filters_by_sport": set(),
         "/milestones": {"category", "limit", "cursor"},
         "/markets": {"event_ticker", "limit", "cursor"},
         "/markets/orderbooks": {"tickers"}}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,150}", value):
        raise Refused("invalid ticker")
    return value


class PublicSportsReader:
    def __init__(self):
        self.count = 0
        self.started = time.monotonic()

    def get(self, path, params):
        if path not in PATHS or set(params)-PATHS[path]:
            raise Refused("sports GET path/query not allowed")
        if self.count >= MAX_REQUESTS or time.monotonic()-self.started > MAX_RUNTIME_SECONDS:
            raise Refused("bounded capture limit reached")
        self.count += 1
        url = ORIGIN + "/trade-api/v2" + path
        if params:
            url += "?" + urlencode(params)
        start, tick = now(), time.monotonic_ns()
        try:
            response = build_opener(NoRedirect).open(Request(url, method="GET", headers={
                "Accept": "application/json", "Accept-Encoding": "identity"}), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_BYTES+1)
            length = response.headers.get("Content-Length")
            return {"url":url,"response_url":response.geturl(),"method":"GET","status":response.status,
                    "path":path,"params":params,"started_at":start,"received_at":now(),
                    "elapsed_ns":time.monotonic_ns()-tick,
                    "content_type":response.headers.get("Content-Type", "").split(";")[0].strip().lower(),
                    "transport_complete":len(raw)<=MAX_BYTES and response.headers.get("Content-Range") is None
                       and (length is None or length.isdigit() and int(length)==len(raw)),
                    "body_base64":base64.b64encode(raw[:MAX_BYTES]).decode(),
                    "body_sha256":hashlib.sha256(raw[:MAX_BYTES]).hexdigest()}


def native_body(r):
    if (r["method"]!="GET" or r["status"]!=200 or r["url"]!=r["response_url"]
            or r["content_type"]!="application/json" or r["transport_complete"] is not True
            or utc(r["received_at"])<utc(r["started_at"])):
        raise Refused("native HTTP/transport evidence refused")
    raw=base64.b64decode(r["body_base64"],validate=True)
    if len(raw)>MAX_BYTES or hashlib.sha256(raw).hexdigest()!=r["body_sha256"]:
        raise Refused("raw receipt integrity")
    data=strict_json(raw)
    if not isinstance(data,dict) or "error" in data or "errors" in data:
        raise Refused("native error envelope")
    return data


def levels(raw):
    if not isinstance(raw,list):
        raise Refused("depth array required")
    result={}
    for item in raw:
        if not isinstance(item,list) or len(item)!=2:
            raise Refused("depth level malformed")
        price,size=map(decimal,item)
        if not 0<=price<=1 or size<=0 or price in result:
            raise Refused("invalid/duplicate depth level")
        result[price]=size
    return sorted(result.items(),reverse=True)


def book(row):
    """Exact native opposing-bid conversion; no midpoint/depth synthesis."""
    identifier(row["ticker"])
    yes=levels(row["orderbook_fp"]["yes_dollars"])
    no=levels(row["orderbook_fp"]["no_dollars"])
    value={"ticker":row["ticker"],"native_quote_timestamp":None,
           "source_time_reason":"REST_SCHEMA_HAS_NO_QUOTE_ASOF_TIMESTAMP",
           "yes_bid":str(yes[0][0]) if yes else None,
           "yes_ask":str(1-no[0][0]) if no else None,
           "no_bid":str(no[0][0]) if no else None,
           "no_ask":str(1-yes[0][0]) if yes else None,
           "yes_bid_depth":[[str(p),str(s)] for p,s in yes],
           "yes_ask_depth":[[str(1-p),str(s)] for p,s in no],
           "no_bid_depth":[[str(p),str(s)] for p,s in no],
           "no_ask_depth":[[str(1-p),str(s)] for p,s in yes]}
    for side in ("yes","no"):
        bid,ask=value[side+"_bid"],value[side+"_ask"]
        value[side+"_spread"]=None if bid is None or ask is None else str(decimal(ask)-decimal(bid))
        if bid is not None and ask is not None and decimal(bid)>decimal(ask):
            raise Refused("crossed book")
    return value


class Capture:
    def __init__(self,store,reader):
        self.store,self.reader=store,reader
        self.run_id=str(uuid.uuid4())
        self.receipts=[]

    def emit(self,kind,payload):
        return self.store.append(str(uuid.uuid4()),kind,{"run_id":self.run_id,**payload})

    def get(self,path,params):
        r=self.reader.get(path,params)
        receipt=self.emit("SPORTS_RAW_HTTP",r)
        self.receipts.append(receipt["hash"])
        return native_body(r),receipt

    def pages(self,path,params,key):
        cursor="";seen=set();rows=[];receipts=[]
        for _ in range(MAX_PAGES):
            query=dict(params)
            if cursor:query["cursor"]=cursor
            data,receipt=self.get(path,query)
            receipts.append(receipt["hash"])
            if set(data)!={key,"cursor"} or not isinstance(data[key],list) or not isinstance(data["cursor"],str):
                raise Refused("unknown pagination completeness")
            rows.extend(data[key]);cursor=data["cursor"]
            if not cursor:
                return rows,receipts
            if cursor in seen:raise Refused("cursor loop")
            seen.add(cursor)
        raise Refused("unterminated pagination at page bound")

    def collect(self):
        self.emit("SPORTS_RUN_START",{"sync_ms":SYNC_MS,"max_quote_age_ms":AGE_MS,
            "capital":"OFF","broker_writes":0,"real_orders_submitted":0,
            "scope":"Soccer, tennis and basketball types; all Sports milestone pages retained; unclassified types explicit"})
        try:
            filters,fr=self.get("/search/filters_by_sport",{})
            if set(filters)!={"filters_by_sports","sport_ordering"}:
                raise Refused("unknown sports filter schema")
            milestones,mpages=self.pages("/milestones",{"category":"Sports","limit":500},"milestones")
            ids=set()
            for m in milestones:
                if not isinstance(m,dict) or not m.get("id") or m["id"] in ids or m.get("category")!="Sports":
                    raise Refused("invalid/duplicate sporting occurrence")
                ids.add(m["id"])
            self.emit("SPORTS_CATALOG",{"milestone_pages":mpages,"milestone_count":len(milestones),
                "filter_receipt":fr["hash"],"pagination_complete":True,"universe_complete":False,
                "reason":"Native competition/type coverage and catalog drift still require qualification"})
            for m in milestones:
                typ=m.get("type","")
                # These are discovery hints only. Unknown types are retained,
                # never silently included as the wrong sport or excluded as complete.
                sport=next((s for s in ("soccer","tennis","basketball") if typ.startswith(s+"_")),None)
                if sport is None:
                    self.emit("SPORTS_UNCLASSIFIED",{"milestone":m,"reason":"unverified sport/type mapping"})
                    continue
                if m.get("end_date") and utc(m["end_date"])<utc(now()):
                    self.emit("SPORTS_OUT_OF_SCOPE",{"milestone_id":m["id"],"reason":"ended according to retained metadata"})
                    continue
                a,b=m.get("primary_event_tickers"),m.get("related_event_tickers")
                if not isinstance(a,list) or not isinstance(b,list):raise Refused("unknown related-event scope")
                event_ids=sorted({identifier(t) for t in a+b})
                if not event_ids:raise Refused("empty sporting-event membership")
                markets={};pages=[]
                for event_id in event_ids:
                    rows,rpages=self.pages("/markets",{"event_ticker":event_id,"limit":200},"markets")
                    pages.extend(rpages)
                    for row in rows:
                        t=identifier(row["ticker"])
                        if row.get("event_ticker")!=event_id or t in markets:raise Refused("market identity/catalog drift")
                        markets[t]=row
                self.emit("SPORTS_EVENT_GROUP",{"milestone":m,"sport_hint":sport,"event_ids":event_ids,
                    "markets":markets,"pages":pages,"related_membership_verified":False,
                    "reason":"Need complete independent native event catalog reconciliation and stable membership"})
                active=sorted(t for t,row in markets.items() if row.get("status") in {"active","open"})
                for offset in range(0,len(active),10):
                    requested=active[offset:offset+10]
                    data,receipt=self.get("/markets/orderbooks",{"tickers":",".join(requested)})
                    if set(data)!={"orderbooks"} or not isinstance(data["orderbooks"],list):raise Refused("book envelope")
                    if len(data["orderbooks"])!=len(requested) or {r["ticker"] for r in data["orderbooks"]}!=set(requested):
                        raise Refused("missing/extra/duplicate requested books")
                    for row in data["orderbooks"]:
                        try:
                            payload={**book(row),"event_id":markets[row["ticker"]]["event_ticker"],
                                     "market_status":markets[row["ticker"]]["status"],
                                     "status_asof":None,"receive_timestamp":receipt["payload"]["received_at"],
                                     "source_receipt":receipt["hash"],"membership_id":m["id"]}
                            self.emit("SPORTS_RAW_QUOTE",payload)
                        except (Refused,KeyError,TypeError) as exc:
                            self.emit("SPORTS_QUOTE_REJECTED",{"receipt":receipt["hash"],"reason":str(exc)})
                self.emit("SNAPSHOT_REJECTED",{"milestone_id":m["id"],"markets":sorted(markets),
                    "reason":"UNPROVEN_MEMBERSHIP_AND_NATIVE_QUOTE_TIME; no interpolation or stale carry-forward",
                    "comparison_performed":False})
            self.emit("SPORTS_RUN_END",{"qualified_snapshots":0,"universe_complete":False,
                      "verdict":"NOT_TESTABLE","stream_adapter_active":False})
        except Exception as exc:
            self.emit("SPORTS_CAPTURE_BLOCKED",{"reason":type(exc).__name__+":"+str(exc)[:250],
                      "receipts":self.receipts,"complete":False,"qualified_snapshots":0})
        return self.store.anchor()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("output_directory",help="New isolated directory; existing paths refused")
    a=p.parse_args()
    out=Path(a.output_directory)
    out.mkdir(parents=False,exist_ok=False)
    store=Store(out/"sports.sqlite")
    capture=Capture(store,PublicSportsReader())
    anchor=capture.collect()
    payload={"schema":"atlas-sports-prospective-raw/1","events":store.events(),"anchor":anchor,
             "native_qualified_dataset":False,"broker_writes":0,"real_orders_submitted":0}
    raw=canonical(payload)
    (out/"SPORTS_PROSPECTIVE_DATASET.json").write_bytes(raw)
    (out/"dataset-integrity.json").write_bytes(canonical({"raw_evidence_sha256":hashlib.sha256(raw).hexdigest(),
                 "native_qualified_dataset_sha256":None,"anchor":anchor}))
    print(canonical({"output":str(out),"anchor":anchor,"qualified_snapshots":0}).decode())


if __name__=="__main__":main()
