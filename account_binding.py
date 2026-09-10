"""Which account and which environment did this durable state come from?

Astra finding A20. Every integrity mechanism in the engine validated
*internal* self-consistency: the ledger schema, the fencing generation, the
continuity hash chain, the bound-state file hashes, the state-epoch marker.
None of them asked the one question that a shared ``DATA_DIR`` makes
answerable only by asking: *whose money is this history about?*

``DATA_DIR`` has no environment segment, so a directory populated by a DEMO
run and later mounted under a PROD run loads cleanly, and so does the
reverse. The seed, the high-water mark, the realized-PnL baseline and the
continuity floor of one account then become the risk envelope of another.
Nothing about that is detectable from the numbers, because the numbers are
perfectly self-consistent -- they are simply about a different account.

The binding is a small, NON-SECRET fingerprint stamped into durable state
and re-checked on load:

``environment``
    ``demo`` / ``prod``. The coarsest and most dangerous distinction.
``api_host``
    Host of the API base URL. Already used elsewhere in the engine as an
    environment proxy, and it is what actually decides where orders go.
``key_fingerprint``
    ``sha256`` of the API **key id** -- the public identifier Kalshi sends
    in the clear as a request header, never the private key. Hashed anyway,
    so an identifier never lands in a state file or a log line.

What this can and cannot distinguish, stated plainly:

* DEMO -> PROD, and PROD -> DEMO: detected by ``environment`` and
  ``api_host``.
* PROD account A -> PROD account B: detected by ``key_fingerprint``, since
  two accounts cannot share an API key id.
* Credential ROTATION on the same account: **indistinguishable** from a
  change of account by any evidence available locally. Kalshi exposes no
  account identifier through any endpoint this engine calls, so there is
  nothing stable to compare. It is therefore treated as a mismatch and
  BLOCKS, and an operator who knows it was a rotation re-binds explicitly
  with ``STATE_BINDING_REBIND_ACK``. Choosing the false positive is
  deliberate: the alternative false negative silently adopts another
  account's loss history.

Nothing here is a secret, and nothing here is a permission: a matching
binding never grants anything, it only removes one reason to refuse.
"""

import hashlib
import os

BINDING_VERSION = 1

#: Operator acknowledgement that a changed fingerprint is the SAME account
#: under rotated credentials. Set to the exact fingerprint being adopted, so
#: an acknowledgement cannot be left armed for whatever comes next.
REBIND_ACK_VAR = "STATE_BINDING_REBIND_ACK"


def _host_of(url: str) -> str:
    if not isinstance(url, str) or "://" not in url:
        return str(url or "")
    return url.split("://", 1)[1].split("/", 1)[0].strip().lower()


def key_fingerprint(key_id) -> str:
    """sha256 of the PUBLIC key identifier, or "" when unknown.

    An unknown key id yields "" rather than a hash of the empty string, so
    "no credential visible" never compares equal to a real credential.
    """
    if not key_id or not isinstance(key_id, str):
        return ""
    return hashlib.sha256(("kalshi-key-id:" + key_id).encode()).hexdigest()


def fingerprint(env: str = None, base_url: str = None, key_id=None) -> dict:
    """The binding of the CURRENT process, as a plain dict."""
    return {"version": BINDING_VERSION,
            "environment": str(env or "").strip().lower(),
            "api_host": _host_of(base_url),
            "key_fingerprint": key_fingerprint(key_id)}


def from_client(client) -> dict:
    """The binding implied by a live broker client."""
    return fingerprint(env=getattr(client, "env", None),
                       base_url=getattr(client, "base_url", None),
                       key_id=getattr(client, "key_id", None))


def digest(binding: dict) -> str:
    """A short stable id for a binding, safe to log."""
    if not isinstance(binding, dict):
        return ""
    parts = "|".join(str(binding.get(k) or "") for k in
                     ("environment", "api_host", "key_fingerprint"))
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def compare(stored, current) -> tuple:
    """``(status, reason)``.

    ``status`` is one of:

    ``"match"``
        Same environment, same host, same credential identity.
    ``"unbound"``
        Nothing was stamped (state written before this check existed). The
        caller adopts the current binding on its next durable write; it is
        NOT a mismatch, but it is not proof of anything either.
    ``"mismatch"``
        The state belongs to a different environment or a different
        account. Blocking.
    ``"credential_changed"``
        Same environment and host, different credential. Locally
        indistinguishable from a different account, so also blocking --
        with a reason that names the operator action that resolves it.
    """
    if not isinstance(stored, dict) or not stored.get("environment"):
        return "unbound", None
    if not isinstance(current, dict):
        return "mismatch", "current binding unavailable"
    if stored.get("environment") != current.get("environment"):
        return "mismatch", (
            f"state was written under environment "
            f"{stored.get('environment')!r} and is being loaded under "
            f"{current.get('environment')!r}")
    if stored.get("api_host") != current.get("api_host"):
        return "mismatch", (
            f"state was written against {stored.get('api_host')!r} and is "
            f"being loaded against {current.get('api_host')!r}")
    if stored.get("key_fingerprint") != current.get("key_fingerprint"):
        return "credential_changed", (
            "the API credential differs from the one this state was written "
            "with: locally this is indistinguishable from a different "
            f"account, so it blocks. If it is a rotation on the SAME "
            f"account, set {REBIND_ACK_VAR}={digest(current)} to re-bind.")
    return "match", None


def rebind_acknowledged(current: dict, env=None) -> bool:
    """True when the operator has explicitly acknowledged this exact
    binding. The acknowledgement names the fingerprint, so it cannot sit
    armed and silently accept a different account later."""
    getter = (env or os.environ).get
    ack = (getter(REBIND_ACK_VAR) or "").strip()
    return bool(ack) and ack == digest(current)
