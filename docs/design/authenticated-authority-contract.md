# Independently authenticated authority contract (A31, A33, M1)

No external authority or broker fence is deployed by this remediation. No key
is implicitly trusted. CAPITAL remains blocked unless all required runtime
proofs are independently verifiable; a passing synthetic test is not an
operator authorization.

## Trust and checkpoint evidence

The application host calls `configure_trust(provider, TrustPolicy(...))` with an
Ed25519 public key, expected authority identity, account fingerprints,
environments, allowed proof purposes, and bounded freshness policy. That
configuration must be independently controlled, outside the restorable economic
volume and outside the provider's response or attributes. Atlas never accepts a
provider-supplied verifier, `trusted=True`, echoed request, or self-selected key.
An integrator must provision the selected authority's pin through a reviewed
host configuration; the repository supplies no production authority selection.

`SignedEvidence` carries a canonical JSON statement and Ed25519 signature with
a versioned domain separator. Atlas independently checks the exact account ID,
broker, environment, account fingerprint, generation, full manifest digest,
fresh 256-bit challenge, authority ID, issuance/expiry, monotonic checkpoint and
purpose-specific claims. Unknown fields at the envelope level, nonfinite or
boolean numeric fields, invalid signatures, expired/future statements and nonce
replays fail closed. Separate purpose permissions prevent account attestations
or freeze responses being accepted as continuity checkpoints. Only signed claims
with the exact expected value can authorize transport-outcome decisions.

The external authority must hold a linearizable checkpoint independently of the
economic state volume. `verify_current` only signs the exact current checkpoint;
`advance` performs compare-and-swap and signs the candidate plus the exact prior
generation/digest. Atlas checks monotonic sequence progression and retains an
in-process high-water mark. The high-water mark is additional protection; it does
not replace external storage. After restart a newly generated nonce and an
independently provisioned trust pin authenticate the external authority's current
checkpoint. Restoring economic files never establishes freshness or installs a
trust root. A signing service that indiscriminately signs caller input does not
satisfy the provider contract, even if its key could be configured by a host.

## Account identity and credential rotation

Persisted economic identity is the canonical broker/environment/stable account
ID. It contains no private key, token, API secret or credential ID. Operator
configuration alone is a proposed identity, not evidence of its correctness.
`account_identity_proven` requires a signed observation from the selected
independent authority, with source `broker_stable_account_identity` or
`independent_account_attestation`, an observation reference, and the matching
stable account. Both ledger CAPITAL eligibility and the broker mutation boundary bind the active
nonsecret credential identifier where available, using the shared
`credential_fingerprint(client)` helper. The ledger also checks the current
client environment against persisted economic identity. That helper reads only
the public API-key identifier, never private signing material; its fingerprint
is an attestation input and is not persisted as economic identity.
Both proof acquisition boundaries recheck the runtime account, credential,
provider, writer lease and state bindings after callbacks. A correctly signed
answer about an earlier binding cannot authorize a changed current binding.

The authority must establish the mapping independently from broker-observed
account information or a reviewed external account attestation. It must not
merely attest the requested label. A credential rotation requires a current
attestation mapping the new credential identifier to the same stable account;
the economic account fingerprint remains unchanged. A different account or
environment produces a different fingerprint and blocks copied state.

The repository's current Kalshi adapter does not implement an authenticated
stable-account discovery endpoint. No unsupported endpoint or capability is
invented here. A selected independent attestation authority is therefore required
before CAPITAL. Its operational selection, account mapping source, key custody,
rotation and revocation remain subject to independent review.

## Broker freeze versus local writer exclusion

`WriterLease` excludes cooperating local engine instances. Transaction locks,
generation checks and exact read sets protect local commits. None of those
facts prove that broker exposure cannot change through another client or broker
process.

A destructive or authority-changing rebase requires all of the following:

1. Current authenticated external continuity and matching independent account
   identity, with clean complete local state and no unresolved intents.
2. The local writer fence and transaction read-set/generation checks, with the
   current local versions included in a fresh challenge.
3. Independently signed broker proof covering **all broker writers**, a stable
   broker watermark, zero orders and positions from an atomic exposure snapshot,
   and a specific broker fence ID in state `HELD`.
4. Broker exclusion that does not expire while an unbounded filesystem commit is
   in progress: `automatic_expiry=false` and explicit fence release only after
   the local commit. Response expiry controls proof freshness; it does not claim
   that an expiring lock remains held forever.
5. Fresh broker proof at commit validation and the existing final exact local
   readback before publication. Uncertain external CAS or failure leaves the
   durable recovery marker and blocks further authority.

Repeated empty pages, read-only broker access, operator booleans, a local lock,
and a timed broker lease without the required release semantics are insufficient.
No current real adapter claims an all-writer broker fence or atomic snapshot.
Rebase therefore remains blocked until a genuinely capable broker/authority
integration is selected and independently reviewed. If that capability is not
available, rebase cannot be enabled by weakening these claims.

## Synthetic evidence

`tests/authority_fixtures.py` and the committed historical harness fixture own
independent test signing keys and pins outside temporary state directories.
Their account and broker statements describe only the isolated synthetic broker.
They do not patch Atlas's verifier, guards or safety assertions. The focused
`test_authenticated_continuity.py` cases include echoed responses, unknown keys,
tampered signed bindings, replay, expiry, sequence rollback, credential rotation,
account/environment mismatch, and local-only or stale broker freeze refusal.
