# V2 ships only the isolated public-data/read-only package.
# Frozen V1 remains at atlas-v1-final-readonly / bd810b4.
FROM python:3.13-slim AS verified
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY v2/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY v2/ ./
# Offline diagnostic tests only; these files never enter the runtime stage.
COPY tools/__init__.py tools/brier_oos.py tools/shadow_pnl.py ./tools/
RUN python -m unittest discover -s tests -v
RUN python mutate.py > mutation_report.json
ARG RAILWAY_GIT_COMMIT_SHA
RUN python -c 'import os,re,json,hashlib,pathlib; sha=os.environ.get("RAILWAY_GIT_COMMIT_SHA",""); assert re.fullmatch("[0-9a-f]{40}",sha), "exact source SHA required"; paths=sorted(pathlib.Path("atlas_v2").glob("*.py")); json.dump({"sha":sha,"source_hashes":{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},"mode":"READ_ONLY","capital":"OFF","model_approved":False},open("release.json","w"),sort_keys=True)'

FROM python:3.13-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PROD_ACCESS_MODE=READ_ONLY CAPITAL=OFF
# Market-data WebSocket and signing dependencies only; no broker SDK.
COPY --from=verified /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=verified /app/atlas_v2 ./atlas_v2
COPY --from=verified /app/release.json /app/mutation_report.json /app/TRAINING_PROTOCOL.json ./
RUN python -c 'from atlas_v2.service import release_identity; release_identity()'
CMD ["python", "-m", "atlas_v2.service"]
