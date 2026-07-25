#!/usr/bin/env python3
"""Dashboard minimal (CLI) : affiche le dernier cycle, les positions
ouvertes et le funnel de decisions depuis les fichiers d'etat JSON.
Volontairement simple ; une UI web est au roadmap (docs/roadmap.md)."""
import json
import os
import sys


def _load(data_dir, name, default):
    p = os.path.join(data_dir, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else \
        os.environ.get("DATA_DIR", ".")
    rep = _load(data_dir, "cycle_report.json", {})
    pos = _load(data_dir, "kalshi_positions.json", {})
    print("=== Atlas Decision Engine — statut ===")
    if not rep:
        print(f"(aucun cycle_report.json dans {data_dir})")
    else:
        keys = ("cycle_id", "scanned_raw", "liquid", "supported",
                "model_evaluated", "positive_edge", "positive_net_ev",
                "risk_passed", "orders_submitted", "fills",
                "cycle_duration_ms")
        for k in keys:
            print(f"  {k:18} {rep.get(k)}")
        rej = rep.get("rejections_by_reason") or {}
        if rej:
            print("  rejets :", json.dumps(rej, ensure_ascii=False))
    print(f"  positions ouvertes : {len(pos)}")
    for t, p in list(pos.items())[:10]:
        print(f"    {t} {p.get('side')} x{p.get('count')} "
              f"@ {p.get('entry_price_cents')}c")


if __name__ == "__main__":
    main()
