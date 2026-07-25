"""
model_gatekeeper.py — v2-ref (2026-07-24)
Verrou final avant tout trading LIVE. Regle I du cahier des charges :
tant que les tests ne passent pas, le programme reste en
DEMO + NO_LIVE_PROMOTION. LIVE n'est JAMAIS active automatiquement.

check_live_allowed() -> (bool, [criteres echoues])
Criteres TOUS obligatoires :
  1. NO_LIVE_PROMOTION != 1 (defaut : 1, donc live bloque par defaut) ;
  2. MODEL_APPROVED_FOR_LIVE=YES (approbation humaine explicite) ;
  3. test_report.json present, date de < 7 jours, failures==0 et errors==0 ;
  4. rapport de validation modele recent (model_validation.json,
     approved=true, < 30 jours).
"""

import os
import json
import time


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_live_allowed():
    failed = []
    if os.getenv("NO_LIVE_PROMOTION", "1").strip() == "1":
        failed.append("NO_LIVE_PROMOTION=1 (defaut) : promotion live "
                      "interdite tant que non levee explicitement")
    if os.getenv("MODEL_APPROVED_FOR_LIVE", "") != "YES":
        failed.append("MODEL_APPROVED_FOR_LIVE=YES absent")
    tr = _load("test_report.json")
    if not tr:
        failed.append("test_report.json absent")
    else:
        if tr.get("failures", 1) != 0 or tr.get("errors", 1) != 0:
            failed.append(f"tests non verts (failures={tr.get('failures')}, "
                          f"errors={tr.get('errors')})")
        try:
            age_d = (time.time() - float(tr.get("generated_ts", 0))) / 86400
            if age_d > 7:
                failed.append(f"test_report.json trop ancien ({age_d:.1f} j)")
        except (TypeError, ValueError):
            failed.append("test_report.json sans generated_ts")
    mv = _load("model_validation.json")
    if not mv or mv.get("approved") is not True:
        failed.append("model_validation.json absent ou non approuve")
    else:
        try:
            age_d = (time.time() - float(mv.get("generated_ts", 0))) / 86400
            if age_d > 30:
                failed.append(f"validation modele trop ancienne ({age_d:.0f} j)")
        except (TypeError, ValueError):
            failed.append("model_validation.json sans generated_ts")
    return (len(failed) == 0), failed
