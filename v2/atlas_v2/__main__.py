"""Offline inspection; explicit public collection requires --collect-once."""
import argparse
import json
from .store import Store
from .data import PublicReader, capture_scan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--collect-once", action="store_true")
    parser.add_argument("--external-anchor")
    args = parser.parse_args()
    store = Store(args.database)
    try:
        if args.collect_once:
            capture_scan(store, PublicReader())
        anchor = json.load(open(args.external_anchor)) if args.external_anchor else None
        print(json.dumps({"verified_anchor": store.verify(anchor), "capital": "OFF", "model_approved": False}))
    finally:
        store.close()


if __name__ == "__main__":
    main()
