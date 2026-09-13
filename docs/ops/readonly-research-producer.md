# Isolated public research producer

The producer is a separate SHADOW_ONLY process and volume. It imports only
standard-library transport and the audited neutral research contract/spool.
There is no execution, risk, order, broker or provider client. Alpha pulls from
the producer; neither the HTTP interface nor Alpha can trigger captures or
alter producer configuration.

Start command: `python tools/readonly_research_run.py`.

Required configuration is an absolute dedicated `DATA_DIR`, `PORT` (default
8080), and `RESEARCH_API_TOKEN` supplied through the existing Alpha token's
Railway variable reference. No new credential is generated. No token is
printed. The process refuses broker credential/authority variable names,
including empty values, and refuses any loaded financial module. It does not
accept AI provider credentials either.

The only outgoing request is a fixed GET of
`https://external-api.kalshi.com/trade-api/v2/markets?status=open&series_ticker=KXBTC15M&limit=10`.
The fixed filters are documented in the official
[Get Markets API](https://docs.kalshi.com/api-reference/market/get-markets).
The request uses verified TLS, no authorization, no environment proxy,
no redirects and a 10-second timeout. The independent collector attempts at
most one capture per minute. Each response is limited to 256 KiB and ten
market objects. This is explicitly a public market sample, not a complete
market, account, position or settlement attestation.

`GET /health` reports process isolation and in-memory operational counters
without disk scans or external requests. `GET /api/research/v1/candidates`
requires an exact bearer token. It returns compatible `rows`, `has_more`,
and `next_cursor` fields. No mutation or control HTTP methods exist. The
listener has eight concurrent request slots and a five-second socket timeout.
Request paths, headers and token values are never logged.

Only unmodified raw market members are passed to audited
`candidate_from_market`. The producer does not borrow event settlement sources,
invent missing market titles, relabel decimal strings, derive missing quotes,
or change the consumer contract. A current exchange schema that lacks required
legacy observations is retained as a capture and refused as a candidate.
Live integration remains blocked until an authentic compatible observation or
a separately reviewed versioned schema adaptation exists.

Each emitted candidate includes exact response bytes in base64, their digest,
the canonical market preimage, its digest, the market JSON pointer, observer
time, source URL and canonical `prod` environment. Its checksum covers this
whole evidence object. The raw bytes are replayed through the same audited
mapping on every HTTP export. A digest detects corruption; it is not an
independent signature by the exchange or a qualified settlement authority.
Transport authentication comes from the live collector's verified TLS and
the authenticated producer connection. TLS private/session secrets are never
retained. Alpha must use `ALPHA_ENVIRONMENT=prod` when consuming these records.

`research_captures/` (32 records, 16 MiB) and `research_spool/` (100 records,
32 MiB) are dedicated rolling opportunity windows, not authoritative ledgers.
Individual stored records are capped at 512 KiB. At capacity, the local
rolling-spool subclass evicts only the oldest complete research records under
the audited capacity lock. Partials count against every bound and are never
evicted as complete records. Failed removal or uncertain capacity refuses the
next write. This local policy avoids six-hour starvation at the first full
spool without changing the audited spool used by existing components.

Pruned opportunities can be missed permanently. This is declared retention,
not silent compaction of prediction or resolution history. Every published
candidate embeds its full raw capture, and Alpha retains that candidate in
prediction source evidence. Thus pruning a producer capture never removes the
only preimage of a retained candidate. The producer never opens historical
Alpha or economic ledgers for writing.

HTTP export holds the same writer reservation lock across scan, read,
validation and real synchronization barriers. A readable file does not imply
durability. Metadata failure, partial read, changed generation, invalid
capture binding or failed synchronization produces HTTP 503. Restart resumes
the collector automatically; existing complete rows are independently
revalidated and synchronized before they are served. Pagination is bounded by
100 retained records; it does not claim an immutable snapshot across requests.
An expired cursor returns refusal instead of silently claiming completeness.

Tests use synthetic bytes and loopback HTTP only:
`python -m unittest tests.test_readonly_research_producer -v`.
No production deployment, financial authority change, live order, provider
request or broker credential read is part of those tests.
