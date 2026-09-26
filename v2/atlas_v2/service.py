"""One bounded public-data collector and cheap read-only health endpoint.

No account credentials, models, research execution, live approval or broker
mutations. V1 volumes are never opened. Storage is an explicit new V2 volume.
"""
import json
import hashlib
import os
from pathlib import Path
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .data import PublicReader, capture_scan
from .domain import Refused, now, utc
from .store import Store


def release_identity(root=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    manifest = json.loads((root / "release.json").read_text())
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", manifest["sha"])
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != manifest["sha"]:
        raise Refused("deployment/image source mismatch")
    actual = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((root / "atlas_v2").glob("*.py"))}
    if not actual or actual != manifest.get("source_hashes"):
        raise Refused("runtime source files differ from image manifest")
    return manifest


def persistent_directory():
    """Reject configuration drift before opening any research database."""
    if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") != "/data":
        raise Refused("dedicated persistent /data volume required before collection")
    data_dir = Path(os.environ.get("ATLAS_V2_DATA_DIR", "/data/atlas-v2"))
    if not data_dir.is_absolute() or data_dir.resolve() != Path("/data/atlas-v2"):
        raise Refused("resolved data directory must be /data/atlas-v2")
    return data_dir


def run():
    identity = release_identity()
    for name in ("KALSHI_PRIVATE_KEY", "KALSHI_KEY_ID", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        if os.environ.get(name):
            raise Refused("V2 public collector must not receive financial/provider credentials")
    if os.environ.get("PROD_ACCESS_MODE", "READ_ONLY") != "READ_ONLY" or os.environ.get("CAPITAL", "OFF") != "OFF":
        raise Refused("read-only mode required")
    data_dir = persistent_directory()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "observations.sqlite")
    state = {"service": "atlas-v2-data", "sha": identity["sha"], "mode": "READ_ONLY",
             "capital": "OFF", "broker_writes": 0, "real_orders_submitted": 0,
             "model_approved": False, "active_models": [], "state": "STARTING",
             "database_path": str(store.path),
             "last_scan_at": None, "last_error": None, "anchor": store.anchor()}
    state_lock = threading.Lock()
    stop = threading.Event()
    print(json.dumps({"at": now(), **state}), flush=True)
    if os.environ.get("ATLAS_V2_EXPORT_ON_START") == "1":
        # Before the collection thread starts: coherent final anchor, private
        # immutable files, no new route or access to any V1/account database.
        from .research_export import write_bundle
        try:
            exported = write_bundle(store.path, data_dir / "exports")
            print(json.dumps({"at":now(), "state":"RESEARCH_EXPORT_READY", "sha":identity["sha"],
                              "capital":"OFF", "broker_writes":0, **exported}), flush=True)
        except Exception as exc:
            print(json.dumps({"at":now(),"state":"RESEARCH_EXPORT_BLOCKED",
                              "reason":type(exc).__name__+":"+str(exc)[:240]}),flush=True)

    def collect():
        reader = PublicReader()
        while not stop.is_set():
            try:
                result = capture_scan(store, reader)
                update = {"state": "COLLECTING_PUBLIC_DATA", "last_scan_at": result["recorded_at"],
                          "last_error": None, "market_count": result["payload"]["market_count"]}
            except Exception as exc:
                update = {"state": "COLLECTION_BLOCKED", "last_error": type(exc).__name__ + ":" + str(exc)[:240]}
            update["anchor"] = store.anchor()
            with state_lock:
                state.update(update)
            print(json.dumps({"at": now(), **update}), flush=True)
            stop.wait(60)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {"/health", "/status"}:
                self.send_error(404)
                return
            with state_lock:
                current = dict(state)
            if current["last_scan_at"] and (utc(now()) - utc(current["last_scan_at"])).total_seconds() > 180:
                current["state"] = "COLLECTION_STALE"
            raw = json.dumps(current).encode()
            self.send_response(200)  # Liveness only, explicitly not qualification.
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass

    threading.Thread(target=collect, daemon=True).start()
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    run()
