"""Bounded semantic mutations. Setup/import/errors never count as kills.

Every witness first passes on an unchanged disposable copy. Mutants run only
in disposable copies under a deny-all socket audit hook. This is not a claim
of exhaustive mutation coverage.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CASE = "test_li06_candle_qualification.TestLi06CandleQualification."
MUTATIONS = [
 ("LM01_CADENCE", "btc_context.py", 'if normalized and ts - normalized[-1]["ts"] != KLINE_INTERVAL_S:', 'if False:', CASE+"test_original_hourly_as_minute_witness"),
 ("LM02_FRESHNESS", "btc_context.py", 'if now - (normalized[-1]["ts"] + KLINE_INTERVAL_S) > MAX_KLINE_CLOSE_AGE_S:', 'if False:', CASE+"test_freshness_exact_boundary"),
 ("LM03_BOOLEAN_NUMBER", "btc_context.py", '((int, float, str) if wire else (int, float))', '((bool, int, float, str) if wire else (bool, int, float))', CASE+"test_boolean_nonfinite_ohlcv"),
 ("LM04_OHLC", "btc_context.py", 'if not (values["low"] <= min(values["open"], values["close"])', 'if False and not (values["low"] <= min(values["open"], values["close"])', CASE+"test_impossible_ohlc_ranges"),
 ("LM05_CACHE_EXPIRY", "btc_context.py", 'def _context_still_fresh(ctx, now):', 'def _context_still_fresh(ctx, now):\n    return True', CASE+"test_cycle_context_cannot_outlive_spot_freshness"),
 ("LM06_OBSERVATION_CUTOFF", "btc_context.py", 'cutoff = now if closed_before is None else _finite_number(closed_before)', 'cutoff = now', CASE+"test_partial_at_request_start_never_promoted_during_parse"),
 ("LM07_PROOF_BYPASS", "btc_context.py", 'def decision_candles_current(model_output, now=None):', 'def decision_candles_current(model_output, now=None):\n    return True', CASE+"test_execution_proof_expiry_and_scalar_identity"),
 ("LM08_ENGINE_GATE", "execution_engine.py", 'def _candle_input_gate(self, dec, report) -> bool:', 'def _candle_input_gate(self, dec, report) -> bool:\n        return True', CASE+"test_exact_engine_gate_keys_off_ticker_even_if_label_renamed"),
 ("LM09_BINANCE_CLOSE", "btc_context.py", 'if opened != int(opened) or closed != opened + 59999:', 'if False:', CASE+"test_binance_contradictory_close_timestamp"),
 ("LM10_KRAKEN_ERROR", "btc_context.py", 'if not isinstance(d, dict) or d.get("error") != []:', 'if False:', CASE+"test_kraken_error_envelope_never_becomes_success"),
 ("LM11_PREVALIDATION_TRUNCATION", "btc_context.py", 'qualify_klines(kl, clock(), closed_before=closed_before)', 'qualify_klines(kl[-limit:], clock(), closed_before=closed_before)', CASE+"test_kraken_malformed_prefix_not_hidden_by_limit"),
 ("LM12_DIGEST", "btc_context.py", '"normalized_sha256": hashlib.sha256(canonical).hexdigest(),', '"normalized_sha256": "0" * 64,', CASE+"test_provenance_bound_to_normalized_rows_and_retained_in_model"),
 ("LM13_RISK_RELEASE", "execution_engine.py", 'if (exec_res.order_id is None and not str(exec_res.status).startswith(\n                    "ambiguous:candle_expired_after_send:")):', 'if exec_res.order_id is None:', CASE+"test_first_attempt_timeout_then_expiry_keeps_half_open_reservation"),
 ("LM14_TRANSPORT_GATE", "kalshi_client.py", 'if not qualified:\n                        raise CandleQualificationExpired(request_started)', 'if False:\n                        raise CandleQualificationExpired(request_started)', "script:transport_sign_stall_witness.py"),
 ("LM15_POSSIBLE_SEND", "kalshi_client.py", 'raise CandleQualificationExpired(request_started)', 'raise CandleQualificationExpired(False)', "script:transport_recheck.py"),
 ("LM16_MANAGER_CLOSURE", "order_manager.py", 'isinstance(e, CandleQualificationExpired) and e.request_started is False:', 'isinstance(e, CandleQualificationExpired):', "script:manager_recheck.py"),
]
BOOTSTRAP = r'''
import contextlib, importlib, io, json, logging, runpy, sys, unittest
network=[]
def deny(event,args):
 if event.startswith("socket."):
  network.append(event); raise RuntimeError("synthetic mutation: network forbidden")
sys.addaudithook(deny)
logging.disable(logging.CRITICAL)
witness=sys.argv[1]
if witness.startswith("script:"):
 class SemanticWitness(unittest.TestCase):
  def runTest(self):
   with contextlib.redirect_stdout(io.StringIO()):
    runpy.run_path("tools/li06_review/"+witness.split(":",1)[1],run_name="__main__")
 suite=unittest.TestSuite([SemanticWitness()])
else:
 suite=unittest.defaultTestLoader.loadTestsFromName(witness)
result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print(json.dumps({"ran":result.testsRun,"failures":[{"test":str(t),"trace":e} for t,e in result.failures],"errors":[{"test":str(t),"trace":e} for t,e in result.errors],"network_attempts":network,"success":result.wasSuccessful()}))
'''


def run(directory, witness):
    env = {"PATH": os.defpath, "PYTHONPATH": str(directory),
           "PYTHONDONTWRITEBYTECODE": "1", "BTC_CONTEXT_CYCLE_CACHE": "0"}
    proc = subprocess.run([sys.executable, "-c", BOOTSTRAP, witness], cwd=directory,
                          env=env, capture_output=True, text=True, timeout=30)
    if proc.returncode:
        return {"infrastructure_error": proc.stderr[-3000:], "returncode":proc.returncode}
    try: return json.loads(proc.stdout)
    except ValueError: return {"infrastructure_error": "non-JSON witness report", "stdout":proc.stdout[-3000:]}


def main():
    output = Path(sys.argv[1]).resolve()
    results=[]
    files=("btc_context.py","btc_probability_model.py","strategy_router.py",
           "execution_engine.py","order_manager.py","kalshi_client.py")
    with tempfile.TemporaryDirectory(prefix="li06-mutants-") as temporary:
        directory=Path(temporary)
        for name in files: shutil.copyfile(ROOT/name,directory/name)
        shutil.copyfile(ROOT/"tests/test_li06_candle_qualification.py",directory/"test_li06_candle_qualification.py")
        scripts=directory/"tools/li06_review";scripts.mkdir(parents=True)
        for name in ("transport_recheck.py","transport_sign_stall_witness.py","manager_recheck.py"):
            shutil.copyfile(ROOT/"tools/li06_review"/name,scripts/name)
        for ident,name,before,after,witness in MUTATIONS:
            path=directory/name;original=path.read_text()
            row={"mutation":ident,"file":name,"witness":witness}
            baseline=run(directory,witness);row["baseline"]=baseline
            if not baseline.get("success") or baseline.get("ran")!=1 or baseline.get("network_attempts"):
                row["classification"]="INCONCLUSIVE_SETUP";results.append(row);continue
            if original.count(before)!=1:
                row["classification"]="NOT_APPLIED";results.append(row);continue
            mutated=original.replace(before,after,1)
            try: compile(mutated,name,"exec")
            except (SyntaxError,IndentationError):
                row["classification"]="INCONCLUSIVE_IMPORT";results.append(row);continue
            path.write_text(mutated)
            result=run(directory,witness);row["mutant"]=result
            path.write_text(original)
            if result.get("infrastructure_error") or result.get("network_attempts"):
                classification="INCONCLUSIVE_INFRASTRUCTURE"
            elif result.get("errors"):
                classification="INCONCLUSIVE_SETUP"
            elif result.get("ran")!=1:
                classification="INCONCLUSIVE_COLLECTION"
            elif result.get("failures"):
                classification="KILLED_BEHAVIORALLY"
            else: classification="SURVIVED"
            row["classification"]=classification;results.append(row)
    report={"scope":"16 bounded invariant-specific mutations; not exhaustive", "results":results,
            "surviving_effective_safety_mutations":sum(r["classification"]=="SURVIVED" for r in results),
            "inconclusive":sum(r["classification"].startswith("INCONCLUSIVE") for r in results),
            "broker_writes":0,"real_provider_requests":0}
    output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"classifications":{r["mutation"]:r["classification"] for r in results},
                      "survivors":report["surviving_effective_safety_mutations"],"inconclusive":report["inconclusive"]},indent=2))
    return int(any(r["classification"]!="KILLED_BEHAVIORALLY" for r in results))

if __name__=="__main__":raise SystemExit(main())
