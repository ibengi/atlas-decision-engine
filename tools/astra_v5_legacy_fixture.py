"""Reproduce an append-only v4 refusal using a supplied exact v4 checkout.

Run with the isolated audit launcher and --v4-source pointing at commit
57d497566b9a218919c5934046edd67e61b9ff43. No network/provider is used. The output
directory must be new; historical ledgers are never edited.
"""
import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--v4-source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source, out = Path(args.v4_source).resolve(), Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(source))
    from config import CFG
    from research_feed import ResearchFeed, candidate_from_market
    from alpha_consumer import SpoolConsumer, ProcessedStore
    from alpha_ledger import AlphaLedger
    from alpha_service import source_binding_for
    CFG.DATA_DIR = str(out)
    market = {"ticker": "SYNTH-V4", "event_ticker": "SYNTH-EVENT",
              "title": "Synthetic refusal recovery?", "rules_primary": "Synthetic rule",
              "settlement_sources": [{"name": "Synthetic authority"}],
              "volume": 10, "open_interest": 4,
              "close_time": "2026-09-11T17:00:00+00:00",
              "expected_expiration_time": "2026-09-11T18:00:00+00:00",
              "yes_bid": 43, "yes_ask": 45, "no_bid": 55, "no_ask": 57}
    candidate = candidate_from_market(market, market, raw_book=market,
                                      observed_at_utc="2026-09-11T12:00:00+00:00")
    record = ResearchFeed(start_writer=False)._build(candidate)
    consumer = SpoolConsumer(store=ProcessedStore(str(out / "processed.jsonl")))
    snapshot = consumer.mint(record)
    ledger = AlphaLedger(str(out / "predictions.jsonl"), str(out / "costs.jsonl"))
    with patch("alpha_ledger._now_iso", return_value="2026-09-11T12:00:01+00:00"):
        ledger.prepare(snapshot.market_snapshot_id, contract_id=snapshot.contract_id,
                       source_record_sha256=record["record_sha256"], environment=CFG.ALPHA_ENVIRONMENT)
        ledger.record_prediction({"prediction_id": "legacy-v4-budget-refusal",
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id, "snapshot": snapshot.as_dict(),
            "state": "BUDGET_EXHAUSTED", "p_meta": None, "executed": False,
            "source_binding": source_binding_for(record, contract_id=snapshot.contract_id,
                market_snapshot_id=snapshot.market_snapshot_id, digest_verified=True)})
    (out / "source-record.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
