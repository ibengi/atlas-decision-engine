"""Local writer exclusion and durable primitives.

Locks are per canonical state root, shared by every cooperating writer.
An advisory lock cannot exclude arbitrary filesystem editors; durable digests
detect their changes. No local artifact proves a whole-volume restore current.
"""
import contextlib
import errno
import fcntl
import hashlib
import os
import tempfile
import threading

from strict_data import loads, dumps


class AuthorityError(RuntimeError):
    pass


_registry_lock = threading.Lock()
_locks = {}
_local = threading.local()
_external = {}
_leases = set()


def _after_fork():
    global _registry_lock, _locks, _local, _external, _leases
    descriptors = list(getattr(_local, "held", {}).values()) + list(_leases)
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass
    _registry_lock = threading.Lock()
    _locks, _external, _leases = {}, {}, set()
    _local = threading.local()


os.register_at_fork(after_in_child=_after_fork)


def configure_authority(path, identity, provider):
    """Bind a runtime provider; no locally persisted proof is accepted."""
    if identity is None or provider is None:
        raise AuthorityError("external authority requires known account identity")
    key = (os.getpid(), root_of(path))
    existing = _external.get(key)
    if existing is not None and existing != (identity, provider):
        raise AuthorityError("conflicting runtime account authority")
    _external[key] = (identity, provider)


def checkpoint(path, identity):
    from continuity_authority import challenge
    m = manifest(path)
    return challenge(identity, m["generation"], hashlib.sha256(dumps(
        m, sort_keys=True, separators=(",", ":")).encode()).hexdigest())


def root_of(path):
    return os.path.realpath(os.path.dirname(os.path.abspath(path)))


def write_all(fd, payload):
    view = memoryview(payload)
    while view:
        try:
            count = os.write(fd, view)
        except InterruptedError:
            continue
        if not isinstance(count, int) or count <= 0 or count > len(view):
            raise OSError(errno.EIO, "write made no valid progress")
        view = view[count:]


def fsync_dir(parent):
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(path, payload):
    parent = root_of(path)
    fd, tmp = tempfile.mkstemp(prefix=".atlas-prepare-", dir=parent)
    try:
        write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        fsync_dir(parent)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def root_lock(path):
    root = root_of(path)
    key = (os.getpid(), root)
    with _registry_lock:
        mutex = _locks.setdefault(key, threading.RLock())
    with mutex:
        held = getattr(_local, "held", {})
        if key in held:
            yield
            return
        os.makedirs(root, exist_ok=True)
        fd = os.open(os.path.join(root, ".atlas-state.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[key] = fd
            _local.held = held
            yield
        finally:
            held.pop(key, None)
            os.close(fd)


class WriterLease:
    """Exclusive process-lifetime engine authority, acquired before loading."""
    def __init__(self, path):
        root = root_of(path)
        os.makedirs(root, exist_ok=True)
        self.pid = os.getpid()
        self.fd = os.open(os.path.join(root, ".atlas-engine-writer.lock"),
                          os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise AuthorityError("another engine owns this economic root") from exc
        _leases.add(self.fd)

    def valid(self):
        return self.fd is not None and self.pid == os.getpid()

    def close(self):
        if self.fd is not None:
            if self.pid == os.getpid():
                _leases.discard(self.fd)
                os.close(self.fd)
            self.fd = None


def manifest_path(path):
    return os.path.join(root_of(path), "state_authority.json")


def manifest(path):
    name = manifest_path(path)
    if not os.path.exists(name):
        return {"version": 1, "generation": 0, "files": {}}
    with open(name, "rb") as fh:
        data = loads(fh.read())
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data.get("version") != 1 or not isinstance(data.get("files"), dict)
            or type(data.get("generation")) is not int or data["generation"] < 0):
        raise AuthorityError("invalid root authority")
    return data


def pending_path(path):
    return os.path.join(root_of(path), "state_transaction.pending")


def recovery_problem(path):
    if os.path.exists(pending_path(path)):
        return "RECOVERY_REQUIRED: incomplete durable transaction"
    try:
        m = manifest(path)
        for name, expected in m["files"].items():
            if os.path.basename(name) != name:
                return "RECOVERY_REQUIRED: invalid authority path"
            target = os.path.join(root_of(path), name)
            with open(target, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != expected:
                return "RECOVERY_REQUIRED: missing, changed or rolled back " + name
    except (OSError, ValueError, AuthorityError) as exc:
        return "RECOVERY_REQUIRED: " + str(exc)
    return None


def remember_file(path, payload):
    m = manifest(path)
    m["generation"] += 1
    m["files"][os.path.basename(path)] = hashlib.sha256(payload).hexdigest()
    durable_replace(manifest_path(path), dumps(m, sort_keys=True).encode())


def begin_write(path):
    durable_replace(pending_path(path), dumps({"version": 1,
        "file": os.path.basename(path), "pid": os.getpid()}).encode())


def finish_write(path):
    os.unlink(pending_path(path))
    fsync_dir(root_of(path))


def active_transaction(path):
    return getattr(_local, "transactions", {}).get((os.getpid(), root_of(path)))


class Transaction:
    """Conservative multi-file commit: interruption requires explicit recovery.

    A durable pending record precedes the first write and is removed only after
    every write/readback succeeds. A failed transaction never becomes eligible
    on restart, including when its first data rename beat its checksum rename.
    """
    def __init__(self, path, allowed=None, owner=None):
        self.path, self.owner = path, owner
        self.allowed = set(allowed) if allowed is not None else None
        self.started = False
        self.failed = False
        self.written = set()
        self.validators = []
        self.stage_json = False
        self.prepared_json = []
        self.staged_generations = {}

    def prepare_json(self, cls, path, data, expected, generation_key, fingerprint):
        if self.allowed is not None and os.path.basename(path) not in self.allowed:
            raise AuthorityError("write outside prepared write set")
        self.prepared_json.append((cls, path, data, expected, generation_key, fingerprint))
        if expected is not None:
            self.staged_generations[path] = expected + 1

    def persist_prepared_json(self):
        self.stage_json = False
        for cls, path, data, expected, generation_key, fingerprint in self.prepared_json:
            if not cls.save(path, data, expect_generation=expected,
                            generation_key=generation_key, expect_fingerprint=fingerprint):
                raise AuthorityError("prepared JSON commit failed")

    def __enter__(self):
        self.lock = root_lock(self.path)
        self.lock.__enter__()
        try:
            from persistence import PersistenceSentinel
            if not PersistenceSentinel.healthy():
                raise AuthorityError("RECOVERY_REQUIRED: persistence failure is latched")
            if active_transaction(self.path) is not None:
                raise AuthorityError("conflicting reentrant authoritative operation")
            issue = recovery_problem(self.path)
            if issue:
                raise AuthorityError(issue)
            self.initial = manifest(self.path)
            self.key = (os.getpid(), root_of(self.path))
            self.external = _external.get(self.key)
            self.previous = None
            if self.external:
                identity, provider = self.external
                self.previous = checkpoint(self.path, identity)
                if provider.verify_current(self.previous) != self.previous:
                    raise AuthorityError("external continuity unproven")
                issue = recovery_problem(self.path)
                if issue:
                    raise AuthorityError(issue)
            transactions = getattr(_local, "transactions", {})
            transactions[self.key] = self
            _local.transactions = transactions
            return self
        except Exception:
            self.lock.__exit__(None, None, None)
            raise

    def writing(self, path):
        if self.failed:
            raise AuthorityError("transaction already failed")
        name = os.path.basename(path)
        if self.allowed is not None and name not in self.allowed:
            self.failed = True
            raise AuthorityError("write outside prepared write set: " + name)
        if not self.started:
            begin_write(self.path)
            self.started = True
        self.written.add(name)

    def __exit__(self, typ, value, traceback):
        try:
            if typ is not None or self.failed:
                # Preserve pending evidence if anything may have reached disk.
                if not self.started and typ is not None:
                    return False
                raise AuthorityError("RECOVERY_REQUIRED: transaction aborted: " + str(value or "write failed")) from value
            elif self.started:
                # Verify every current authority entry, ignoring only OUR marker.
                m = manifest(self.path)
                for name, expected in m["files"].items():
                    with open(os.path.join(root_of(self.path), name), "rb") as fh:
                        if hashlib.sha256(fh.read()).hexdigest() != expected:
                            raise AuthorityError("commit readback mismatch: " + name)
                if self.external:
                    identity, provider = self.external
                    candidate = checkpoint(self.path, identity)
                    if provider.advance(self.previous, candidate) != candidate:
                        raise AuthorityError("external checkpoint commit uncertain")
                for validate in self.validators:
                    if validate() is not True:
                        raise AuthorityError("commit authorization expired or changed")
                # External callbacks and commit validators may themselves fail
                # or observe changes. Recheck every exact byte at publication.
                if manifest(self.path) != m:
                    raise AuthorityError("authority changed during final validation")
                for name, expected in m["files"].items():
                    with open(os.path.join(root_of(self.path), name), "rb") as fh:
                        if hashlib.sha256(fh.read()).hexdigest() != expected:
                            raise AuthorityError("commit evidence changed: " + name)
                finish_write(self.path)
        finally:
            _local.transactions.pop(self.key, None)
            self.lock.__exit__(typ, value, traceback)
