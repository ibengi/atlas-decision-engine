"""Launch audited Python with only a small, credential-free environment."""
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parent
repo = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve()
deps = os.environ.get('ATLAS_AUDIT_DEPENDENCIES', '')
env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
paths = [str(root/'offline'), str(repo)]
if deps and pathlib.Path(deps).is_dir(): paths.insert(1, deps)
env.update(PYTHONPATH=os.pathsep.join(paths),
           PYTHONDONTWRITEBYTECODE='1', PROBE_PROVIDERS_ON_START='0',
           KALSHI_DEMO_KEY_ID='audit-dummy', KALSHI_DEMO_PRIVATE_KEY='audit-dummy',
           ATLAS_AUDIT_ALLOW_LOOPBACK='1' if sys.argv[3] == 'run_tests.py' else '0')
output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='atlas-isolated-run-') as data:
    env['DATA_DIR'] = data
    with output.open('w') as f:
        p = subprocess.run([sys.executable] + sys.argv[3:], cwd=repo, env=env,
                           stdout=f, stderr=subprocess.STDOUT)
print('AUDIT_SUBPROCESS_EXIT=' + str(p.returncode))
sys.exit(p.returncode)
