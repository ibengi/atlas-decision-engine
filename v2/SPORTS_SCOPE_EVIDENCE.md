# Sports provider permission evidence

This is evidence-verification support, not permission approval or a deployed
transport proof. No genuine provider artifact is enrolled in this candidate.
The production trust-pin set is deliberately empty. Synthetic pins exist only
inside tests. Plain user confirmation, copied claims, a provider-looking URL,
and a SHA-256 calculated by the submitter are never sufficient authority.

## Trust and enrollment

An authorized reviewer must inspect the original redacted provider-issued
permission export or authenticated provider UI capture. Verify the origin
independently through the provider session or attributable provider receipt;
a screenshot with a logo alone is insufficient. Preserve an immutable origin
receipt documenting that verification, plus the original redacted artifact.
Reject private-key/signature material, screenshots without provider identity,
unverifiable copies, ambiguous key identity or absent permission details.

The provider evidence must establish the installed key identifier, exactly
`read`, and no write, trade or transfer authority. Use the full identifier only
in the controlled verification context; never log it or copy it into GitHub.
Its stable public binding is lowercase SHA-256 of the UTF-8 bytes of
`atlas-sports-key-id-v1:` followed by the exact key identifier (no whitespace
normalization or partial-key matching). This is not a hash of the private key.

Create this exact redacted manifest after verification:

| Field | Required value |
| --- | --- |
| schema | `atlas-sports-provider-scope/1` |
| provider | `Kalshi` |
| source_url | exact authenticated provider source; current allowlist is the official account profile or API-key endpoint |
| artifact_sha256 | SHA-256 of preserved redacted provider artifact bytes |
| origin_receipt_sha256 | SHA-256 of independently preserved origin-verification receipt |
| key_id_sha256 | domain-separated fingerprint defined above |
| scopes | `["read"]` |
| write_allowed / trade_allowed / transfer_allowed | JSON `false`, each explicitly established by evidence |
| observed_at | UTC time the permissions were observed, not the later submission time |
| expires_at | UTC expiry, at most 24 hours after observation |

Do not fabricate provider-native fields or claim the normalized manifest is
provider-signed. The manifest is a reviewed index to provider evidence. Its
authority derives from independently verified provenance and code review,
not from its own JSON claims. Preserve both referenced artifacts for audit.

Compute SHA-256 of `domain.canonical(manifest)`. Enrollment adds ONLY that hash
to `REVIEWED_EVIDENCE_SHA256` in `sports_scope_evidence.py` through reviewed code.
Retain artifact/receipt references in the review evidence. Enrollment requires
tests, exact-SHA CI and a new deployment authorization. Runtime variables and
the mounted volume cannot add trusted hashes. No enrollment CLI or approval
flag exists. Revocation removes the pin in a new reviewed release; do not edit
old evidence. A pin cannot prove permissions have not subsequently changed;
the 24-hour maximum is a conservative local policy, not a provider guarantee.

## Runtime

The optional non-secret `ATLAS_V2_SPORTS_SCOPE_EVIDENCE` variable carries the
redacted manifest (maximum 8192 UTF-8 bytes). All fields and hashes, the pinned
canonical manifest digest, installed-key binding and UTC validity are checked.
Extra fields, including private keys or signatures, are rejected. No artifact
bytes, credentials or supplied JSON are persisted. The scope event records
only validated attribution, fingerprint, hashes, timestamps and fallback reason.

The authenticated `/api_keys` check remains first. Exact read-only API evidence
needs no fallback. A 403, 404, transport connection failure, or matching key
record with omitted/null scopes permits the fallback check. A 401, missing or
duplicate key, revoked/disabled key, explicit broader/empty scopes, malformed
or incomplete envelope, late error and exhausted budget fail closed. Provider
API evidence that contradicts the fallback always wins as a rejection.

This narrowly scoped fallback supplies only scope authority. It does not prove
WebSocket authentication, subscription success, native timing, clock bounds,
membership, market edge or financial permission. The existing financial guard,
Sports-only channel allowlist, bounded runtime, R01–R12, 250 ms synchronization
and 1 second age rules remain unchanged. In particular the independent
`SOURCE_CLOCK_BOUND_UNPROVEN` gap remains; scope evidence cannot close it.

## Verification and deployment

`PYTHONPATH=v2 python -m unittest discover -s v2/tests`

`python v2/mutate.py`

Tests use synthetic provider manifests and patched trust pins solely to prove
behavior. They include tampering, unknown pins, wrong installed key, permissions,
source spoofing, expiry/future timestamps, private fields, malformed JSON,
API contradiction, fallback precedence and startup integration.

Deploy only an explicitly authorized new exact SHA to atlas-v2-data in temporary
probe-only mode. No deployment is authorized by this implementation request.
Restore exact learning release `55cd4530dc4fc143f9c041eceaada9a1ba88bef4`, its
predeploy gate and probe flag off after any separately authorized bounded run.

Official API reference checked for the existing first-choice scope path:
https://docs.kalshi.com/api-reference/api-keys/get-api-keys
