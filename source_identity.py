"""Pure settlement-source identity and retained preimage verification.

No execution, configuration or I/O dependency. This proves supported source
structure and truthful normalization, not authentication of an exchange feed.
Legacy rendered text alone cannot reconstruct discarded source structure.
"""

#: The identity keys a settlement-source OBJECT may carry. Both are text and
#: both belong to the authority's identity: `name` says WHO settles the
#: market, `url` says WHERE that authority publishes the number. RA-02: a
#: normalization that keeps the first and drops the second is not a
#: canonical identity, it is a lossy rendering.
SOURCE_IDENTITY_KEYS = ("name", "url")
SOURCE_CONTAINER_SCHEMA = "atlas-settlement-source-v1"
SOURCE_ALIAS_KEYS = ("settlement_sources", "settlement_source")

#: Characters the rendering below escapes so that the text form is INJECTIVE:
#: distinct structured identities must never render to the same string, or the
#: comparison that detects contradictory aliases is comparing renderings
#: rather than facts (RA-02).
_RENDER_ESCAPES = {"\\": "\\\\", "|": "\\|", "<": "\\<", ">": "\\>"}


class MalformedSettlementSource(Exception):
    """A settlement-source container this producer cannot claim to understand.

    Raised rather than returned so that "the exchange published nothing" and
    "the exchange published something we cannot read" stay different facts
    inside this module, even though both end as an ABSENT `resolution_source`
    in the record. The distinction is what stops a partly-read member from
    being reported as a fully-read one (RA-01).
    """


def _identity_text(value, key: str):
    """Non-blank text for one identity key, `None` when JSON-absent, or raise.

    A JSON `null` is how a feed says "not published", so it reads as ABSENT.
    Anything else that is not non-blank text is MALFORMED: an integer URL is
    a plausible internal field and a wholly implausible publication location,
    and a blank one is "published an empty URL", which is not "published no
    URL".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise MalformedSettlementSource(
            f"{key} is {type(value).__name__}, not text")
    text = value.strip()
    if not text:
        raise MalformedSettlementSource(f"{key} is blank")
    return text


def settlement_source_identity(value):
    """The CANONICAL STRUCTURED identity of a settlement source, or None.

    Returns a tuple of MEMBERS, each member a tuple of sorted `(key, text)`
    pairs -- so collection boundaries and per-member URLs both survive into
    the value that gets compared and rendered.

    RA-01: EVERY MEMBER, AND THE WHOLE CONTAINER, BEFORE NORMALIZATION.
        The previous version looped `for key in ("name", "url")` and RETURNED
        on the first key that was present and readable. With `name` present,
        `url` was never examined at all, so

            {"name": "CF Benchmarks RTI", "url": 8080}

        was accepted as the authority "CF Benchmarks RTI" while the malformed
        half of the same object went unread. AA-02's own rule -- one malformed
        member taints the collection -- was correct and simply never reached.

        Here there is no early return. Every identity key present on a member
        is validated, every member of a list is validated, and a container
        whose shape this producer does not recognise -- a mapping with no
        identity key, a bare number, a boolean -- is MALFORMED rather than
        quietly empty.

    RA-02: STRUCTURE IS THE IDENTITY.
        `", ".join(names)` destroyed exactly the two things that distinguish
        two authorities. `[{"name": "A"}, {"name": "B"}]` (two authorities)
        and `[{"name": "A, B"}]` (one authority whose name contains a comma)
        rendered identically, and the URL was dropped altogether -- so two
        alias keys naming one authority at two DIFFERENT locations compared
        EQUAL, and `resolve_alias` resolved a real contradiction silently.
    """
    if isinstance(value, bool):
        raise MalformedSettlementSource("a boolean is not a settlement source")
    if isinstance(value, str):
        text = _identity_text(value, "name")
        return ((("name", text),),) if text else ()
    if type(value) is dict:
        # This version supports exactly name/url objects and a single list
        # of such objects or strings. Unknown extensions cannot be silently
        # discarded from the identity. A future extension needs a declared
        # schema and a lossless canonical representation before admission.
        if any(type(key) is not str or key not in SOURCE_IDENTITY_KEYS
               for key in value):
            raise MalformedSettlementSource(
                "unsupported settlement-source identity key")
        fields = []
        for key in SOURCE_IDENTITY_KEYS:
            if key not in value:
                continue
            text = _identity_text(value[key], key)
            if text is not None:
                fields.append((key, text))
        if not fields:
            raise MalformedSettlementSource(
                "a settlement-source object requires a name or url")
        return (tuple(sorted(fields)),)
    if type(value) is list:
        members = []
        for item in value:
            # No early exit on success and none on failure either: a
            # malformed member raises, which taints the whole collection,
            # because a settlement source list that is half readable is not
            # half true.
            if type(item) not in (str, dict):
                raise MalformedSettlementSource(
                    "settlement-source members must be text or objects")
            members.extend(settlement_source_identity(item))
        return tuple(members)
    raise MalformedSettlementSource(
        f"{type(value).__name__} is not a settlement source")


def _escape_identity(text: str) -> str:
    return "".join(_RENDER_ESCAPES.get(ch, ch) for ch in text)


def render_settlement_source(identity) -> str:
    """Readable AND injective text for a canonical structured identity.

    `name <url>`, members joined by ` | `, with the backslash, pipe and
    angle-bracket characters escaped inside every name and URL. The escaping is what makes the
    rendering injective: without it, one authority literally named `A | B`
    and two authorities `A` and `B` would produce the same record field, and
    RA-02 would be re-opened in the rendering after being closed in the
    comparison.
    """
    parts = []
    for member in identity:
        fields = dict(member)
        name = fields.get("name")
        url = fields.get("url")
        if name and url:
            parts.append(f"{_escape_identity(name)} "
                         f"<{_escape_identity(url)}>")
        elif url:
            parts.append(f"<{_escape_identity(url)}>")
        elif name:
            parts.append(_escape_identity(name))
    return " | ".join(parts)


def settlement_source_comparator(value):
    """What `resolve_alias` compares two `resolution_source` aliases BY.

    The STRUCTURED identity, never the rendering: a comparator that flattens
    is a comparator that reports disagreement as agreement.

    Every malformed container compares equal (to `None`), because "we cannot
    read this" is one fact however it is misspelled -- and a field both
    aliases agree is unreadable is ABSENT, which the contract refuses anyway.
    """
    try:
        return settlement_source_identity(value) or None
    except MalformedSettlementSource:
        return None


def _settlement_source_name(value):
    """The settlement authority the exchange PUBLISHED, as text, or None.

    Kalshi records settlement sources as a list of objects. This reads what it
    published -- every member, and the URL as well as the name -- and never
    invents one, never falls back to the exchange's own name because the
    market is listed there, and never treats an empty name as a reason to
    report the URL as the name.
    """
    try:
        identity = settlement_source_identity(value)
    except MalformedSettlementSource:
        return None
    if not identity:
        return None
    return render_settlement_source(identity) or None


def _retained_alias_identity(aliases):
    if type(aliases) is not dict or not aliases or any(
            type(key) is not str or key not in SOURCE_ALIAS_KEYS for key in aliases):
        raise MalformedSettlementSource("unsupported source alias container")
    identified = []
    for key in SOURCE_ALIAS_KEYS:
        if key not in aliases:
            continue
        value = aliases[key]
        # Null and the empty alias string are explicitly absent under the
        # historical source contract; retain their bytes without inventing an
        # authority. An empty list is supplied structure, not absent text.
        if value is None or (type(value) is str and value == ""):
            continue
        identified.append((key, settlement_source_identity(value)))
    if not identified or not identified[0][1]:
        raise MalformedSettlementSource("source aliases identify no authority")
    identity = identified[0][1]
    if any(other != identity for _key, other in identified[1:]):
        raise MalformedSettlementSource("retained source aliases contradict")
    return identified[0][0], identity


def source_evidence_from_market(market):
    """Retain every supplied alias, without flattening source boundaries."""
    aliases = {key: market[key] for key in SOURCE_ALIAS_KEYS if key in market}
    _retained_alias_identity(aliases)

    def copy(value):
        if type(value) is dict:
            return dict(value)
        if type(value) is list:
            return [copy(member) for member in value]
        return value

    return {"schema": SOURCE_CONTAINER_SCHEMA,
            "aliases": {key: copy(value) for key, value in aliases.items()}}


def verify_settlement_source_evidence(record):
    """Verify source preimage structure, alias equality, rendering and path.

    Missing evidence is UNVERIFIED, including historical v3 records. A
    historical digest authenticates the stored normalized preimage only; it
    cannot restore an unsupported source identity the old producer discarded.
    """
    refused = lambda reason: {"verified": False, "reason": reason}
    if type(record) is not dict:
        return refused("source record is not an object")
    evidence = record.get("settlement_source_evidence")
    if evidence is None:
        return refused("versioned settlement source preimage is missing")
    if type(evidence) is not dict or set(evidence) != {"schema", "aliases"} \
            or evidence.get("schema") != SOURCE_CONTAINER_SCHEMA:
        return refused("unsupported settlement source evidence schema")
    try:
        alias, identity = _retained_alias_identity(evidence["aliases"])
        rendered = render_settlement_source(identity)
    except (MalformedSettlementSource, TypeError, ValueError):
        return refused("retained settlement source structure is invalid")
    if type(record.get("resolution_source")) is not str or \
            record["resolution_source"] != rendered:
        return refused("retained source and normalized authority disagree")
    provenance = record.get("field_provenance")
    if type(provenance) is not dict or provenance.get("resolution_source") != \
            "market." + alias:
        return refused("retained source does not bind its provenance alias")
    return {"verified": True, "reason": "supported source identity preimage verified"}
