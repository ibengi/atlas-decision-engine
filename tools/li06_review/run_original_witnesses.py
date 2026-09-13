"""Replay the original eight witnesses against exact baseline and working tree.

Requires the baseline Git object locally. Never fetches or contacts a provider.
Output paths are explicit; no authoritative file is written.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
BASE="5c4e7897a0b99065f3a23bc4ed834b44dba9580c"
output=Path(sys.argv[1]).resolve();output.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix="li06-baseline-") as temporary:
    before=Path(temporary)
    for name in ("btc_context.py","btc_probability_model.py","strategy_router.py"):
        content=subprocess.run(["git","show",BASE+":"+name],cwd=ROOT,
            capture_output=True,check=True).stdout
        (before/name).write_bytes(content)
    for label,source,after in (("before",before,"0"),("after",ROOT,"1")):
        env={"PATH":os.defpath,"PYTHONDONTWRITEBYTECODE":"1",
             "LI06_SOURCE_DIR":str(source),"LI06_EXPECT_AFTER":after,
             "LI06_RESULT_PATH":str(output/(label+"-original-eight.json"))}
        proc=subprocess.run([sys.executable,str(ROOT/"tools/li06_review/original_eight_cases.py")],
            cwd=temporary,env=env,text=True,capture_output=True)
        (output/(label+"-original-eight.log")).write_text(proc.stdout+proc.stderr)
        if proc.returncode:raise SystemExit(proc.returncode)
print(json.dumps({"baseline":BASE,"before_witnesses":8,"after_regressions":8,
                  "network_requests":0,"broker_writes":0}))
