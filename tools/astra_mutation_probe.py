#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproducible negative-control mutation runner (Astra AA-17). SHADOW ONLY.

Astra's finding was not "two bugs"; it was that the suite could not TELL. Two
safety mutations passed all 1,670 tests, because the assertions were about
counters and diagnostics rather than about what ended up in the ledger.

This tool re-runs that experiment on demand. For each mutation it:

  1. copies the repository into a throwaway directory (the working tree is
     NEVER modified -- a mutation runner that edits the real source and
     restores it afterwards is one crash away from committing a mutation);
  2. applies one textual mutation that re-introduces a specific finding;
  3. runs the tests that are supposed to detect it;
  4. records pytest phases and a reviewed invariant-specific assertion;
  5. reports a behavioral kill, diagnostic-only result, survival, or an
     explicit inconclusive category.

A SURVIVED line is a gap in the suite, not a bug in this tool. So is an
INCONCLUSIVE one: see `_classify` below for why a non-zero pytest exit is
not by itself a behavioural kill (RA-15).

    python tools/astra_mutation_probe.py            # every mutation
    python tools/astra_mutation_probe.py --only M04
    python tools/astra_mutation_probe.py --json

Exit code 0 means every effective mutation has a behavioral witness, known
diagnostic controls are classified honestly, and no experiment is inconclusive
or unapplied. Exit code 1 means that evidence gate is not satisfied.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: id -> (description, file, old text, new text, detecting test selectors)
#:
#: Each `old` string is asserted to be present before the mutation is applied,
#: so a refactor that moves the code makes this tool fail loudly rather than
#: silently reporting KILLED for a mutation it never managed to apply.
MUTATIONS = {
    "M01": (
        "re-introduce the substituted settlement source (\"kalshi\")",
        "research_feed.py",
        '        if field == "resolution_source" and value is not None:\n'
        '            value = _settlement_source_name(value)\n'
        '            if value is None:\n'
        '                key = None\n',
        '        if field == "resolution_source":\n'
        '            value = _settlement_source_name(value) or "kalshi"\n'
        '            key = key or "settlement_sources"\n',
        ["tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts"],
    ),
    "M02": (
        "re-introduce the 0.0 volume default",
        "research_feed.py",
        '    for field in ("volume", "open_interest"):\n'
        '        if facts.get(field) is None:\n'
        '            continue\n',
        '    for field in ("volume", "open_interest"):\n'
        '        if facts.get(field) is None:\n'
        '            facts[field] = 0.0\n'
        '            provenance[field] = provenance_path(field, field)\n'
        '            unavailable = [f for f in unavailable if f != field]\n'
        '            continue\n',
        ["tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts"],
    ),
    "M03": (
        "use the close time as the expected resolution time",
        "candidate_contract.py",
        '    "expected_resolution_time_utc": ("market", ("expected_expiration_time",\n'
        '                                                "expiration_time")),',
        '    "expected_resolution_time_utc": ("market", ("expected_expiration_time",\n'
        '                                                "expiration_time",\n'
        '                                                "close_time")),',
        ["tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts",
         "tests/test_astra_aa01_aa18_remediation.py::AA03_ContradictoryAliasesSilentlyChosen"],
    ),
    "M04": (
        "ignore per-field provenance (SURVIVED in the rejected candidate)",
        "candidate_contract.py",
        "        for field in REQUIRED_FIELDS:\n"
        "            _check_field_provenance(field, record, provenance, unavailable,\n"
        "                                    errors)\n",
        "        for field in REQUIRED_FIELDS:\n"
        "            pass\n",
        ["tests/test_astra_mutation_regression.py::M04_IgnorePerFieldProvenance"],
    ),
    "M05": (
        "exclude provenance from the checksum",
        "candidate_contract.py",
        '    return {k: v for k, v in record.items() if k != "record_sha256"}',
        '    return {k: v for k, v in record.items()\n'
        '            if k not in ("record_sha256", "field_provenance",\n'
        '                         "quote_observation", "unavailable_fields")}',
        ["tests/test_astra_mutation_regression.py::M05_ProvenanceExcludedFromTheChecksum"],
    ),
    "M06": (
        "accept legacy v1 records",
        "candidate_contract.py",
        'LEGACY_FEED_SCHEMAS = ("atlas-research-candidate-v1",\n'
        '                       "atlas-research-candidate-v2")',
        'LEGACY_FEED_SCHEMAS = ()',
        ["tests/test_astra_mutation_regression.py::M11_AcceptLegacyWhileKeepingDiagnostics",
         "tests/test_alpha_feed_readiness.py"],
    ),
    "M07": (
        "allow a missing book side",
        "candidate_contract.py",
        "    for field in QUOTE_FIELDS:\n"
        "        kind = observation.get(field)\n",
        "    for field in ():\n"
        "        kind = observation.get(field)\n",
        ["tests/test_astra_v3_remediation.py::M07P_CompleteRecordWithDerivedQuotes",
         "tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts"],
    ),
    # M07 deletes the quote-observation check entirely. M07P is the harder
    # variant the re-audit asked for: leave the check in place and simply
    # accept `derived` as admissible evidence. A complete, correctly
    # checksummed, correctly attributed record that says its quotes were
    # derived must still produce ZERO snapshots and ZERO durable predictions.
    "M07P": (
        "accept a complete record whose quotes are declared DERIVED",
        "candidate_contract.py",
        "        elif kind != QUOTE_OBSERVED:\n",
        "        elif kind not in QUOTE_OBSERVATION_KINDS:\n",
        ["tests/test_astra_v3_remediation.py::M07P_CompleteRecordWithDerivedQuotes",
         "tests/test_astra_aa01_aa18_remediation.py::AA01_DerivedQuotesArePresentedAsObserved"],
    ),
    "M08": (
        "add a forbidden execution import to the producer",
        "research_feed.py",
        "from config import CFG\n",
        "from config import CFG\nimport order_manager  # noqa: F401\n",
        ["tests/test_research_feed_boundary.py",
         "tests/test_alpha_safety_boundary.py"],
    ),
    "M09": (
        "let a producer exception propagate into the decision cycle",
        "research_feed.py",
        "        except Exception as e:                                # noqa: BLE001\n"
        "            self.rejected += 1\n"
        "            # AA-10 (re-audit): DEFERRED, not logged. `log.warning` here runs\n"
        "            # the handler on the engine's thread, and the handler writes to\n"
        "            # the same volume the fsync was moved off.\n"
        "            self._note(logging.WARNING,\n"
        '                       f"[RESEARCH_FEED] candidate dropped: "\n'
        '                       f"{type(e).__name__}: {e}")\n'
        "            return False\n",
        "        except Exception:                                     # noqa: BLE001\n"
        "            raise\n",
        ["tests/test_astra_mutation_regression.py::M09_ProducerExceptionPropagation",
         "tests/test_alpha_automatic_feed.py"],
    ),
    "M10": (
        "overwrite historical bytes instead of appending",
        "durable_append.py",
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)",
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)",
        ["tests/test_astra_mutation_regression.py::M10_OverwriteHistoricalBytes",
         "tests/test_astra_aa01_aa18_remediation.py::AA12_ShortWritesAndTornTails"],
    ),
    "M11": (
        "accept legacy while preserving the diagnostics "
        "(SURVIVED in the rejected candidate)",
        "alpha_consumer.py",
        '        if record.get("schema") in LEGACY_FEED_SCHEMAS:\n'
        '            self.stats["legacy_schema"] += 1\n'
        '            self.last_refusal = f"legacy schema {record.get(\'schema\')!r}"\n'
        '            log.warning("[ALPHA_CONSUMER] refusing legacy feed record "\n'
        '                        f"{record.get(\'schema\')!r}: it may carry substituted "\n'
        '                        f"market facts and must be re-observed")\n'
        "            return None\n",
        '        if record.get("schema") in LEGACY_FEED_SCHEMAS:\n'
        '            self.stats["legacy_schema"] += 1\n'
        '            log.warning("[ALPHA_CONSUMER] refusing legacy feed record "\n'
        '                        f"{record.get(\'schema\')!r}: it may carry substituted "\n'
        '                        f"market facts and must be re-observed")\n'
        '            record = dict(record, schema=FEED_SCHEMA)\n'
        '            record["record_sha256"] = __import__(\n'
        '                "candidate_contract").compute_checksum(record)\n',
        ["tests/test_astra_mutation_regression.py::M11_AcceptLegacyWhileKeepingDiagnostics"],
    ),
    # ── v3 re-audit: one mutation per newly closed invariant ────────────
    "M12": (
        "coerce a numeric settlement-source member into a name (AA-02)",
        "research_feed.py",
        "    if isinstance(value, bool) or not isinstance(value, str):\n"
        "        raise MalformedSettlementSource(\n"
        "            f\"{key} is {type(value).__name__}, not text\")\n"
        "    text = value.strip()\n",
        "    text = str(value).strip()\n",
        ["tests/test_astra_v3_remediation.py::AA02_NumericSettlementMembersAreCoercedToText",
         "tests/test_astra_v4_remediation.py::RA01_ValidNameSkippedTheRestOfTheContainer"],
    ),
    "M13": (
        "file a contradiction as an ordinary absence (AA-03)",
        "research_feed.py",
        "        except AliasContradiction as exc:\n"
        "            contradictions[field] = {k: _diagnostic(v)\n"
        "                                     for k, v in exc.values.items()}\n"
        "            facts[field] = None\n"
        "            # Deliberately NOT appended to `unavailable_fields`.\n"
        "            continue\n",
        "        except AliasContradiction:\n"
        "            facts[field] = None\n"
        "            unavailable.append(field)\n"
        "            continue\n",
        ["tests/test_astra_v3_remediation.py::AA03_ContradictionWasDowngradedToAbsence"],
    ),
    "M14": (
        "count only complete files against the spool bound (AA-11)",
        "research_spool.py",
        '        if capacity["occupied"] >= self.max_records or \\\n',
        '        if capacity["records"] >= self.max_records or \\\n',
        ["tests/test_astra_v3_remediation.py::AA11_PartialWritesEscapedTheBound"],
    ),
    "M15": (
        "treat readable bytes as proof of a durable commit (AA-13)",
        "alpha_ledger.py",
        "        return self.committed_prediction(market_snapshot_id) is not None\n",
        "        return self.find_prediction_by_analysis(\n"
        "            analysis_identity(market_snapshot_id)) is not None\n",
        ["tests/test_astra_v3_remediation.py::AA13_ReadableBytesWereTreatedAsCommitted"],
    ),
    "M16": (
        "accept a partial settlement binding as verified (AA-15)",
        "alpha_resolution_ingest.py",
        "        missing = _missing_binding(row[\"supplied_binding\"], committed)\n",
        "        missing = []\n",
        ["tests/test_astra_v3_remediation.py::AA15_PartialBindingWasAcceptedAsVerified"],
    ),
    "M17": (
        "trust an unqualified settlement source by default (AA-15)",
        "alpha_resolution_ingest.py",
        "        if not allowed:\n",
        "        if False:\n",
        ["tests/test_astra_v3_remediation.py::AA15b_UnqualifiedSourcesWereTrustedByDefault"],
    ),
    "M18": (
        "let the processed store skip the durable append protocol (AA-12)",
        "alpha_consumer.py",
        "            with serialized_append(self.path) as append:\n"
        "                append(line)\n",
        "            fd = os.open(self.path,\n"
        "                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n"
        "            try:\n"
        "                os.write(fd, line.encode(\"utf-8\"))\n"
        "                os.fsync(fd)\n"
        "            finally:\n"
        "                os.close(fd)\n",
        ["tests/test_astra_v3_remediation.py::AA12_ProcessedStoreBypassedTheDurableProtocol"],
    ),
    "M19": (
        "raise instead of failing closed on a malformed number (NEW-01)",
        "candidate_contract.py",
        "    except Exception as exc:                                  # noqa: BLE001\n"
        "        return [f\"record could not be validated ({type(exc).__name__}: \"\n"
        "                f\"{exc}); a value the contract cannot classify is refused\"]\n",
        "    except Exception:                                         # noqa: BLE001\n"
        "        raise\n",
        ["tests/test_astra_v3_remediation.py::NEW01_MalformedNumbersRaiseInsteadOfFailingClosed"],
    ),
    "M20": (
        "dispatch to providers without a durable PREPARE (AA-13)",
        "alpha_service.py",
        "            return self._defer_without_dispatch(\n"
        "                snapshot, f\"prepare_not_durable: {type(e).__name__}\")\n",
        "            pass\n",
        ["tests/test_astra_v3_remediation.py::AA13c_PrepareFailureDidNotStopDispatch"],
    ),
    "M21": (
        "re-dispatch an analysis that was already committed (AA-13)",
        "alpha_service.py",
        "        if recovered is not None:\n"
        "            return self._acknowledge_recovered(snapshot, recovered)\n",
        "        if False:\n"
        "            return self._acknowledge_recovered(snapshot, recovered)\n",
        ["tests/test_astra_v3_remediation.py::AA13b_RestartPaidTwiceForACommittedAnalysis"],
    ),
    "M22": (
        "leave an unserialized appender on a shared file (AA-14)",
        "alpha_ledger.py",
        "        with self.log.lock():\n"
        "            existing = self.find_invalidation(prediction_id)\n",
        "        if True:\n"
        "            existing = self.find_invalidation(prediction_id)\n",
        ["tests/test_astra_v3_remediation.py::AA14_UnserializedAppendersAndStaleCaches"],
    ),
    "M23": (
        "protect a guessed processed-state path instead of the real one (AA-16)",
        "alpha_learning_runtime.py",
        "    store_path = getattr(processed_store, \"path\", None)\n"
        "    if store_path:\n"
        "        paths.setdefault(os.path.realpath(store_path), \"processed ledger\")\n"
        "    # The configured path, resolved exactly as `ProcessedStore` resolves it.\n"
        "    paths.setdefault(os.path.realpath(_p(CFG.ALPHA_STATE_FILE)),\n"
        "                     \"processed ledger\")\n",
        "",
        ["tests/test_astra_v3_remediation.py::AA16_ProtectionMissedTheConfiguredProcessedPath"],
    ),
    "M24": (
        "drop the source evidence the digest describes (AA-15)",
        "alpha_service.py",
        "        \"source_evidence\": json.loads(json.dumps(evidence, default=str))\n"
        "        if evidence else {},\n",
        "        \"source_evidence\": {},\n",
        ["tests/test_astra_v3_remediation.py::AA15c_SourceDigestCouldNotBeReverifiedAfterPruning"],
    ),
    "M25": (
        "log synchronously on the engine's own thread (AA-10)",
        "research_feed.py",
        "        try:\n"
        "            self.writer.note(level, message)\n",
        "        try:\n"
        "            log.log(level, message)\n",
        ["tests/test_astra_v3_remediation.py::AA10_ResearchLoggingStillBlockedTheCycle",
         # RA-03 moved the refusal diagnostics onto the writer, which left
         # `_admit`'s missing-container note as the only thing the observer
         # still says -- and M25 SURVIVED the whole suite until a case drove
         # that branch. This is it.
         "tests/test_astra_v4_remediation.py::RA03_TheObserverThreadStillHashedAndValidated"],
    ),
    # ── v4 counter-audit: one mutation per RA finding ──────────────
    #
    # RA-15 asks specifically for M26, the directory-fsync barrier, because
    # RA-05 closed that hole and the only tests behind it asserted an
    # exception type. The rest are here for the same reason the first
    # twenty-six are: a control nobody has tried to break is a control
    # nobody knows the strength of.
    "M26": (
        'swallow the directory fsync failure, so an undurable NAME reads as a durable append (RA-05)',
        "durable_append.py",
        '        fd = os.open(parent, os.O_RDONLY)\n'
        '    except OSError as exc:\n'
        '        raise DurabilityUnknown(\n'
        '            f"cannot open {parent} to persist the directory entry: {exc}"\n'
        '        ) from exc\n'
        '    try:\n'
        '        os.fsync(fd)\n'
        '    except OSError as exc:\n'
        '        raise DurabilityUnknown(\n'
        '            f"cannot fsync {parent}; the bytes are durable but the NAME they "\n'
        '            f"live under is not: {exc}") from exc',
        '        fd = os.open(parent, os.O_RDONLY)\n'
        '    except OSError:\n'
        '        return\n'
        '    try:\n'
        '        os.fsync(fd)\n'
        '    except OSError:\n'
        '        pass',
        [
         "tests/test_astra_v4_remediation.py::RA05_UnknownDurabilityWasReportedAsSuccess",
         "tests/test_astra_v4_remediation.py::RA15_TheDirectoryFsyncBarrierIsAssertedBySemantics",
        ],
    ),
    "M27": (
        'return on the first present settlement-source key, leaving the rest of the member unvalidated (RA-01)',
        "research_feed.py",
        '        fields = []\n'
        '        for key in SOURCE_IDENTITY_KEYS:\n'
        '            if key not in value:\n'
        '                continue\n'
        '            text = _identity_text(value[key], key)\n'
        '            if text is not None:\n'
        '                fields.append((key, text))',
        '        fields = []\n'
        '        for key in SOURCE_IDENTITY_KEYS:\n'
        '            if key not in value:\n'
        '                continue\n'
        '            text = _identity_text(value[key], key)\n'
        '            if text is not None:\n'
        '                return ((("name", text),),)',
        [
         "tests/test_astra_v4_remediation.py::RA01_ValidNameSkippedTheRestOfTheContainer",
        ],
    ),
    "M28": (
        'compare settlement-source aliases by the flattened comma-joined names again (RA-02)',
        "research_feed.py",
        '    try:\n'
        '        return settlement_source_identity(value) or None\n'
        '    except MalformedSettlementSource:\n'
        '        return None',
        '    names = []\n'
        '    try:\n'
        '        for member in settlement_source_identity(value):\n'
        '            names.append(dict(member).get("name") or "")\n'
        '    except MalformedSettlementSource:\n'
        '        return None\n'
        '    return ", ".join(names) or None',
        [
         "tests/test_astra_v4_remediation.py::RA02_StructuredSourceIdentitiesWereFlattenedBeforeComparison",
        ],
    ),
    "M29": (
        "serialize, hash and validate the record on the engine's observer thread again (RA-03)",
        "research_feed.py",
        '            admitted = self._admit(candidate)\n'
        '            if admitted is None:\n'
        '                return False\n'
        '            return self.writer.offer_candidate(\n'
        '                admitted, approx_bytes=self._size(admitted))',
        '            admitted = self._build(candidate)\n'
        '            if admitted is None:\n'
        '                return False\n'
        '            return self.writer.offer(\n'
        '                admitted, approx_bytes=self._size(admitted))',
        [
         "tests/test_astra_v4_remediation.py::RA03_TheObserverThreadStillHashedAndValidated",
        ],
    ),
    "M30": (
        'classify spool entries with a second, disagreeing stat again (RA-04)',
        "research_spool.py",
        '            ours = name.endswith(TEMP_SUFFIX) or name.endswith(RECORD_SUFFIX)\n'
        '            if not stat.S_ISREG(st.st_mode):\n'
        '                if ours:\n'
        '                    raise SpoolCapacityUnknown(\n'
        '                        f"{name} carries a spool suffix but is not a regular "\n'
        '                        f"file (mode {st.st_mode:#o}); how much of the budget "\n'
        '                        f"it occupies cannot be established")\n'
        '                continue\n'
        '            entry = (name, st.st_size, st.st_mtime)',
        '            if not os.path.isfile(path):\n'
        '                continue\n'
        '            entry = (name, st.st_size, st.st_mtime)',
        [
         "tests/test_astra_v4_remediation.py::RA04_TheSecondStatCouldDisagreeWithTheFirst",
        ],
    ),
    "M31": (
        'let an unreadable budget row make recorded spend SMALLER instead of unknown (RA-06)',
        "alpha_cost.py",
        '            except ValueError:\n'
        '                # RA-06 -- AN UNREADABLE ROW MAKES THE TOTAL UNKNOWN, NOT\n'
        '                # SMALLER.\n'
        '                #\n'
        '                # Before, a torn LAST line ended the read (`break`) and an\n'
        '                # unparsable line anywhere else was logged and SKIPPED. Both\n'
        '                # produce the same thing: a spend total that is too low, in\n'
        '                # the file the daily cap is enforced against. A guard that\n'
        '                # under-counts is a guard that does not bind.\n'
        '                #\n'
        '                # `check()` already refuses when the ledger cannot be READ.\n'
        '                # It has to refuse just as firmly when the ledger can be read\n'
        '                # and not believed, so this raises the same exception.\n'
        '                raise RuntimeError(\n'
        '                    f"budget ledger row {i + 1} of {self.path} is not "\n'
        '                    f"readable JSON, so total spend cannot be established; "\n'
        '                    f"no provider call is made until an operator reconciles "\n'
        '                    f"it. The row is PRESERVED, never rewritten.")',
        '            except ValueError:\n'
        '                if i == len(lines) - 1:\n'
        '                    break\n'
        '                log.error(f"[ALPHA_BUDGET] unparsable row at line {i + 1}")\n'
        '                continue',
        [
         "tests/test_astra_v4_remediation.py::RA06_TheBudgetLedgerBypassedTheDurableProtocol",
        ],
    ),
    "M32": (
        'discard the durable prediction row the ledger returned and keep the generated id (RA-07)',
        "alpha_gateway.py",
        '                durable_id = str((durable or {}).get("prediction_id") or "")\n'
        '                if durable_id and durable_id != opportunity["prediction_id"]:\n'
        '                    log.warning(\n'
        '                        f"[ALPHA_LEDGER] this analysis is already recorded as "\n'
        '                        f"{durable_id}; the generated id "\n'
        '                        f"{opportunity[\'prediction_id\']} names no row and is "\n'
        '                        f"kept only as a diagnostic")\n'
        '                    opportunity["generated_prediction_id"] = \\\n'
        '                        opportunity["prediction_id"]\n'
        '                    opportunity["prediction_id"] = durable_id',
        '                pass',
        [
         "tests/test_astra_v4_remediation.py::RA07_TheGeneratedIdWasAcknowledgedInsteadOfTheDurableOne",
        ],
    ),
    "M33": (
        'treat readable PREPARE bytes as a durable announcement on a retry (RA-08)',
        "alpha_ledger.py",
        '                if analysis_id in self.prepare_commits():\n'
        '                    return existing\n'
        "                # Readable bytes with no receipt: the previous attempt's\n"
        '                # append landed and its fsync did not. Refusing here would\n'
        '                # strand the snapshot forever, and returning would repeat the\n'
        '                # defect. Finish the durability instead: this append fsyncs\n'
        '                # the whole file, so the row above becomes durable at the same\n'
        '                # moment its receipt does. If that fails too, it raises and\n'
        '                # nothing is dispatched.\n'
        '                log.warning(\n'
        '                    f"[ALPHA_LEDGER] {analysis_id} has a PREPARE row with no "\n'
        '                    f"receipt; completing its durability instead of trusting "\n'
        '                    f"the bytes")\n'
        '                self._commit_prepare(analysis_id, market_snapshot_id)\n'
        '                return existing',
        '                return existing',
        [
         "tests/test_astra_v4_remediation.py::RA08_PrepareDurabilityWasNotRecheckedOnRetry",
        ],
    ),
    "M34": (
        'patch the processed cache and re-stamp its generation AFTER the append lock is released (RA-09)',
        "alpha_consumer.py",
        '        try:\n'
        '            with serialized_append(self.path) as append:\n'
        '                append(line)\n'
        '                # RA-09: inside the lock, and an INVALIDATION rather than a\n'
        '                # patch. While this lock is held no other writer can append,\n'
        '                # so clearing the generation here cannot be stamped past\n'
        "                # somebody else's row. Patching the cache and re-stamping\n"
        '                # afterwards could, and did.\n'
        '                self._cache = None\n'
        '                self._generation = None\n'
        '        except (OSError, TimeoutError) as e:\n'
        '            # If we cannot remember that we processed this, a restart will\n'
        '            # process it again and pay for it again. Loud, and the caller\n'
        '            # stops consuming this cycle.\n'
        '            raise RuntimeError(f"processed status not durable: {e}")',
        '        try:\n'
        '            with serialized_append(self.path) as append:\n'
        '                append(line)\n'
        '        except (OSError, TimeoutError) as e:\n'
        '            raise RuntimeError(f"processed status not durable: {e}")\n'
        '        if self._cache is not None:\n'
        '            self._cache[snapshot_id] = row\n'
        '            self._generation = self._current_generation()',
        [
         "tests/test_astra_v4_remediation.py::RA09_TheProcessedCacheAdvancedPastAnotherWritersRow",
        ],
    ),
    "M35": (
        'promote a recovered spend refusal to a terminal ANALYZED (RA-10)',
        "alpha_service.py",
        '        non_terminal = state in NON_TERMINAL_STATES\n'
        '        if non_terminal:\n'
        '            self.telemetry.incr("recovered_non_terminal")\n'
        '            log.warning(\n'
        '                f"[ALPHA_SERVICE] the committed row for "\n'
        '                f"{snapshot.contract_id} is {state}, which means no provider "\n'
        '                f"was ever asked; it is acknowledged DEFERRED, not ANALYZED, "\n'
        '                f"so the observation is retried rather than discarded")\n'
        '        try:\n'
        '            self.consumer.store.mark(\n'
        '                snapshot.market_snapshot_id,\n'
        '                STATUS_DEFERRED if non_terminal else STATUS_ANALYZED,\n'
        '                contract_id=snapshot.contract_id,\n'
        '                detail=(f"recovered_non_terminal: {state}" if non_terminal\n'
        '                        else "recovered_committed_prediction"),\n'
        '                prediction_id=prediction_id)',
        '        non_terminal = False\n'
        '        try:\n'
        '            self.consumer.store.mark(\n'
        '                snapshot.market_snapshot_id, STATUS_ANALYZED,\n'
        '                contract_id=snapshot.contract_id,\n'
        '                detail="recovered_committed_prediction",\n'
        '                prediction_id=prediction_id)',
        [
         "tests/test_astra_v4_remediation.py::RA10_ABudgetRefusalTurnedTerminalOnRecovery",
        ],
    ),
    "M36": (
        'make the environment and the contract version optional in a settlement binding again (RA-11)',
        "alpha_resolution_ingest.py",
        'REQUIRED_BINDING = ("contract_id", "market_snapshot_id",\n'
        '                    "source_record_sha256", "environment",\n'
        '                    "contract_schema")',
        'REQUIRED_BINDING = ("contract_id", "market_snapshot_id",\n'
        '                    "source_record_sha256")',
        [
         "tests/test_astra_v4_remediation.py::RA11_TheRequiredSettlementBindingWasIncomplete",
        ],
    ),
    "M37": (
        "qualify a settlement without recomputing the prediction's retained evidence (RA-12)",
        "alpha_resolution_ingest.py",
        '        evidence = verify_source_evidence(prediction)\n'
        '        if not evidence["verified"]:\n'
        '            detail = {"row": index, "prediction_id": prediction_id,\n'
        '                      "source_evidence": evidence,\n'
        '                      "reason": f"the prediction\'s retained source evidence "\n'
        '                                f"does not independently recompute to the "\n'
        '                                f"digest it claims, so this settlement "\n'
        '                                f"cannot be qualified: {evidence[\'reason\']}"}\n'
        '            result["evidence_unverified"].append(detail)\n'
        '            result["quarantined"].append(dict(detail))\n'
        '            result["rejected"].append(dict(detail))\n'
        '            continue\n'
        '\n'
        '        existing = ledger.find_resolution(prediction_id)',
        '        evidence = verify_source_evidence(prediction)\n'
        '\n'
        '        existing = ledger.find_resolution(prediction_id)',
        [
         "tests/test_astra_v4_remediation.py::RA12_RetainedEvidenceWasNotVerifiedAtSettlement",
        ],
    ),
    "M38": (
        'let the learning report score every resolution, qualified or not (RA-13)',
        "alpha_learning.py",
        '    rows = ledger.qualified_resolved()',
        '    rows = ledger.resolved()',
        [
         "tests/test_astra_v4_remediation.py::RA13_LearningConsumedUnqualifiedSettlements",
        ],
    ),
    "M39": (
        "let the Meta engine's calibration lookup weight unqualified outcomes (RA-13)",
        "alpha_ledger.py",
        '        for row in self.qualified_resolved():',
        '        for row in self.resolved():',
        [
         "tests/test_astra_v4_remediation.py::RA13_LearningConsumedUnqualifiedSettlements",
        ],
    ),
    "M40": (
        'drop the budget ledger from the report-overwrite guard (RA-14)',
        "alpha_learning_runtime.py",
        '    from alpha_cost import BUDGET_LEDGER_FILE\n'
        '    budget_path = getattr(budget_ledger, "path", None)\n'
        '    if budget_path:\n'
        '        paths.setdefault(os.path.realpath(budget_path), "budget ledger")\n'
        '    paths.setdefault(os.path.realpath(_p(BUDGET_LEDGER_FILE)),\n'
        '                     "budget ledger")\n'
        '    paths.setdefault(\n'
        '        os.path.realpath(os.path.join(directory, BUDGET_LEDGER_FILE)),\n'
        '        "budget ledger")',
        '    pass',
        [
         "tests/test_astra_v4_remediation.py::RA14_TheBudgetLedgerWasNotProtectedFromTheReport",
        ],
    ),
}


# v5 invariant-preserving mutation ports. The original counterexamples above
# remain readable, while these anchors target the current shared guards.
# M16/M36 model illicitly inventing omitted binding data; M37 defeats the
# shared source proof so a second independent call cannot hide the mutation.
_V5_PORTS = {'M09': ('research_feed.py',
         '        except Exception:                                     # noqa: BLE001\n'
         '            self.rejected += 1\n'
         '            # AA-10 (re-audit): DEFERRED, not logged. `log.warning` here runs\n'
         "            # the handler on the engine's thread, and the handler writes to\n"
         '            # the same volume the fsync was moved off.\n'
         '            self._note(logging.WARNING,\n'
         '                       "[RESEARCH_FEED] candidate admission refused")\n'
         '            return False',
         '        except Exception:\n            raise',
         None),
 'M12': ('source_identity.py',
         '    if isinstance(value, bool) or not isinstance(value, str):\n'
         '        raise MalformedSettlementSource(\n'
         '            f"{key} is {type(value).__name__}, not text")\n'
         '    text = value.strip()\n',
         '    text = str(value).strip()\n',
         None),
 'M16': ('alpha_resolution_ingest.py',
         '        missing = _missing_binding(row["supplied_binding"], committed)',
         '        row["supplied_binding"] = {**committed, **row["supplied_binding"]}\n'
         '        missing = _missing_binding(row["supplied_binding"], committed)',
         None),
 'M18': ('alpha_consumer.py',
         '            with serialized_append(self.path) as append:\n'
         '                self._observed_file = True\n'
         '                append(line)\n',
         '            fd = os.open(self.path,\n'
         '                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n'
         '            try:\n'
         '                os.write(fd, line.encode("utf-8"))\n'
         '                os.fsync(fd)\n'
         '            finally:\n'
         '                os.close(fd)\n',
         None),
 'M21': ('alpha_service.py',
         '        if recovered is not None and recovered.get("state") not in NON_TERMINAL_STATES:\n'
         '            return self._acknowledge_recovered(snapshot, recovered)',
         '        if False:\n            return self._acknowledge_recovered(snapshot, recovered)',
         None),
 'M27': ('source_identity.py',
         '        fields = []\n'
         '        for key in SOURCE_IDENTITY_KEYS:\n'
         '            if key not in value:\n'
         '                continue\n'
         '            text = _identity_text(value[key], key)\n'
         '            if text is not None:\n'
         '                fields.append((key, text))',
         '        fields = []\n'
         '        for key in SOURCE_IDENTITY_KEYS:\n'
         '            if key not in value:\n'
         '                continue\n'
         '            text = _identity_text(value[key], key)\n'
         '            if text is not None:\n'
         '                return ((("name", text),),)',
         None),
 'M28': ('source_identity.py',
         '        return tuple(members)',
         '        return ((("name", ", ".join(dict(member).get("name", "") for member in '
         'members)),),)',
         None),
 'M31': ('alpha_cost.py',
         '            except (ValueError, TypeError, RuntimeError) as exc:\n'
         '                raise RuntimeError(f"budget ledger row {i + 1} is invalid: {exc}") from '
         'exc',
         '            except (ValueError, TypeError, RuntimeError):\n                continue',
         None),
 'M33': ('alpha_ledger.py',
         '        try:\n'
         '            if not sync_path(self.path, expected_generation=generation):\n'
         '                raise LedgerError("ledger disappeared before its durability barrier")\n'
         '        except OSError as exc:\n'
         '            raise LedgerError(f"alpha ledger durability remains unconfirmed: {exc}") '
         'from exc',
         '        return None',
         ['tests/test_astra_v5_recovery.py::V5Recovery::test_persistent_prepare_failure_seven_polls_and_recreated_service_never_dispatches',
          'tests/test_astra_v5_durability.py']),
 'M34': ('alpha_consumer.py',
         '        try:\n'
         '            with serialized_append(self.path) as append:\n'
         '                self._observed_file = True\n'
         '                append(line)\n'
         '                # RA-09: inside the lock, and an INVALIDATION rather than a\n'
         '                # patch. While this lock is held no other writer can append,\n'
         '                # so clearing the generation here cannot be stamped past\n'
         "                # somebody else's row. Patching the cache and re-stamping\n"
         '                # afterwards could, and did.\n'
         '                self._cache = None\n'
         '                self._generation = None\n'
         '        except (OSError, TimeoutError) as e:\n'
         '            # If we cannot remember that we processed this, a restart will\n'
         '            # process it again and pay for it again. Loud, and the caller\n'
         '            # stops consuming this cycle.\n'
         '            raise RuntimeError(f"processed status not durable: {e}")',
         '        try:\n'
         '            with serialized_append(self.path) as append:\n'
         '                append(line)\n'
         '        except (OSError, TimeoutError) as e:\n'
         '            raise RuntimeError(f"processed status not durable: {e}")\n'
         '        if self._cache is not None:\n'
         '            self._cache[snapshot_id] = row\n'
         '            self._generation = self._current_generation()',
         None),
 'M35': ('alpha_service.py',
         '        non_terminal = state in NON_TERMINAL_STATES',
         '        non_terminal = False',
         None),
 'M36': ('alpha_resolution_ingest.py',
         '        committed = _prediction_binding(prediction)\n'
         '        missing = _missing_binding(row["supplied_binding"], committed)',
         '        committed = _prediction_binding(prediction)\n'
         '        for key in ("environment", "contract_schema"):\n'
         '            row["supplied_binding"].setdefault(key, committed.get(key))\n'
         '        missing = _missing_binding(row["supplied_binding"], committed)',
         None),
 'M37': ('alpha_settlement_validation.py',
         '    verdict = {"verified": False, "claimed": "", "recomputed": None,\n'
         '               "reason": ""}\n'
         '    try:',
         '    verdict = {"verified": True, "claimed": "", "recomputed": None,\n'
         '               "reason": ""}\n'
         '    return verdict\n'
         '    try:',
         None)}
for _key, (_file, _old, _new, _selectors) in _V5_PORTS.items():
    _description, *_unused, _original_selectors = MUTATIONS[_key]
    MUTATIONS[_key] = (_description, _file, _old, _new,
                      _selectors or _original_selectors)


# Independent v5 negative controls, including the retained M26P.
MUTATIONS.update({'M26P': ('remove the actual directory synchronization primitive (exact prior barrier-deletion '
          'variant, RA-05)',
          'durable_append.py',
          'def fsync_directory(parent: str) -> None:\n',
          'def fsync_directory(parent: str) -> None:\n    return None\n',
          ['tests/test_astra_v5_durability.py::V4RA05DirectoryRetry',
           'tests/test_astra_v4_remediation.py::RA15_TheDirectoryFsyncBarrierIsAssertedBySemantics']),
 'M41': ('count failed pytest summaries as behavioral kills without structured evidence (V4-RA-18)',
         'tools/astra_mutation_probe.py',
         '    detail = {"counts": _outcomes(proc.stdout), "semantic_witnesses": []}\n',
         '    detail = {"counts": _outcomes(proc.stdout), "semantic_witnesses": []}\n'
         '    if proc.returncode == 1:\n'
         '        return "KILLED_BEHAVIORALLY", detail\n',
         ['tests/test_astra_v5_mutations.py::V4RA18TruthfulMutationEvidence']),
 'M42': ('discard unsupported structured source extensions (V4-RA-01/V4-RA-02)',
         'source_identity.py',
         '        if any(type(key) is not str or key not in SOURCE_IDENTITY_KEYS\n'
         '               for key in value):',
         '        if False:',
         ['tests/test_astra_v5_producer.py::V5SourceSchema']),
 'M43': ('treat transient spool directory ENOENT as empty capacity (V4-RA-04)',
         'research_spool.py',
         '            names = os.listdir(self.directory)\n        except OSError as exc:',
         '            names = os.listdir(self.directory)\n'
         '        except FileNotFoundError:\n'
         '            return [], []\n'
         '        except OSError as exc:',
         ['tests/test_astra_v5_producer.py::V5CapacityUncertainty']),
 'M44': ('admit a resolution before its prediction (V4-RA-14 chronology)',
         'alpha_settlement_validation.py',
         '        if resolved_at <= predicted_at:\n'
         '            raise ValueError("resolution must strictly follow prediction")',
         '        if False:\n'
         '            raise ValueError("resolution must strictly follow prediction")',
         ['tests/test_astra_v5_binding.py::SettlementQualificationTests']),
 'M45': ('admit impossible future resolution chronology (V4-RA-14)',
         'alpha_settlement_validation.py',
         '        if resolved_at > current or predicted_at > current:\n'
         '            raise ValueError("prediction or resolution is in the future")',
         '        if False:\n'
         '            raise ValueError("prediction or resolution is in the future")',
         ['tests/test_astra_v5_binding.py::SettlementQualificationTests']),
 'M46': ('wait for a contended research queue mutex on the engine hook (V4-RA-03)',
         'research_spool.py',
         '            if not self._queue.mutex.acquire(blocking=False):',
         '            if not self._queue.mutex.acquire(blocking=True):',
         ['tests/test_astra_v5_producer.py::V5ObserverIsolation::test_contended_accounting_or_queue_mutex_drops_without_wait']),
 'M47': ('bind source A to economically different snapshot B (V4-RA-17)',
         'alpha_settlement_validation.py',
         '        for field, value in expected.items():\n'
         '            if _canonical(observed.get(field)) != _canonical(value):',
         '        for field, value in expected.items():\n            if False:',
         ['tests/test_astra_v5_binding.py::SettlementQualificationTests',
          'tests/test_astra_v5_recovery.py::V5Recovery::test_snapshot_b_with_same_labels_and_other_quotes_is_refused']),
 'M48': ('leave a concurrency gap between provider accounting and prediction publication (v5 '
         'self-adversarial)',
         'alpha_service.py',
         '            with exclusive_lock(lock_path, timeout=10.0):\n'
         '                return self._analyze_one_locked(snapshot, record)',
         '            if True:\n                return self._analyze_one_locked(snapshot, record)',
         ['tests/test_astra_v5_recovery.py::V5Recovery::test_two_services_cannot_redispatch_between_usage_and_prediction_commit'])})

for _key, _selector in {
    "M28": "tests/test_astra_mutation_regression.py::M28_StructuredIdentityConsumerWitness",
    "M35": "tests/test_astra_mutation_regression.py::M35_RefusalAcknowledgementWitness",
}.items():
    _description, _file, _old, _new, _selectors = MUTATIONS[_key]
    MUTATIONS[_key] = (_description, _file, _old, _new, [_selector, *_selectors])

MUTATIONS.update({'M49': ('allow lossy historical source evidence into settlement learning (V4-RA-02/V4-RA-17)',
         'alpha_settlement_validation.py',
         '        if not source_identity["verified"]:',
         '        if False:',
         ['tests/test_astra_v5_binding.py::SettlementQualificationTests::test_historical_lossy_source_without_preimage_stays_immutable_unqualified']),
 'M50': ('drop atomic runtime path registration during derived-report publication (V4-RA-15)',
         'alpha_learning_runtime.py',
         '        with publication_guard():',
         '        with __import__("contextlib").nullcontext():',
         ['tests/test_astra_v5_binding.py::RuntimePathProtectionTests::test_registration_during_publication_cannot_lose_new_runtime_state'])})

MUTATIONS.update({'M01P': ('fabricate a default settlement authority before retained source/provenance capture '
          '(stronger M01 variant)',
          'research_feed.py',
          '    market = market if isinstance(market, dict) else {}\n',
          '    market = dict(market) if isinstance(market, dict) else {}\n'
          '    if not any(market.get(key) for key in ("settlement_sources", '
          '"settlement_source")):\n'
          '        market["settlement_sources"] = [{"name": "kalshi"}]\n',
          ['tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts'])})


def _copy_repo(destination):
    def _ignore(directory, names):
        return {n for n in names
                if n in (".git", "__pycache__", ".pytest_cache", "venv",
                         ".venv", "node_modules")}
    shutil.copytree(REPO, destination, ignore=_ignore, symlinks=True)


#: RA-15 -- A NON-ZERO EXIT CODE IS NOT A BEHAVIOURAL KILL.
#:
#: The runner used to report KILLED for `returncode != 0`. pytest exits
#: non-zero for several reasons that say nothing about whether the mutation
#: changed behaviour: a collection error because the mutation broke an import
#: the detecting module needs, a usage error because a selector no longer
#: resolves, an internal error, a fixture that raised in setUp. Every one of
#: those reads as "the suite noticed" while proving only that something went
#: wrong somewhere -- which is exactly the false-green class AA-17 exists for,
#: pointed at the negative control instead of at the code.
#:
#: So a kill now requires an actual test body to have FAILED, with no
#: collection or setup errors, and the counts are reported. Anything else is
#: INCONCLUSIVE and counted as a survivor, because a run that proves nothing
#: is not evidence of safety.
SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|"
                        r"xpassed|deselected|warning|warnings|"
                        r"subtests passed|subtests failed)")


def _outcomes(output: str) -> dict:
    """pytest's own final summary line, parsed. `{}` when there is none."""
    for line in reversed((output or "").strip().splitlines()):
        found = SUMMARY_RE.findall(line)
        if found and ("passed" in line or "failed" in line
                      or "error" in line):
            counts = {}
            for number, name in found:
                key = "errors" if name == "error" else name
                counts[key] = counts.get(key, 0) + int(number)
            return counts
    return {}


STATUSES = frozenset({
    "KILLED_BEHAVIORALLY", "DIAGNOSTIC_ONLY", "INCONCLUSIVE_SETUP",
    "INCONCLUSIVE_COLLECTION", "INCONCLUSIVE_IMPORT",
    "INCONCLUSIVE_INFRASTRUCTURE", "SURVIVED", "NOT_APPLIED",
})

# These two mutations alter a redundant refusal branch.  They have semantic
# controls, but the controls still refuse all unsafe inputs.  A wording or
# counter assertion failing is therefore diagnostic evidence only.
DIAGNOSTIC_MUTATIONS = frozenset({"M06", "M17"})


# The original late-default M01 is now independently blocked by the retained
# source preimage validator. Keep that exact mutation and report its survival
# honestly. Exemption from the EFFECTIVE survivor count requires both named
# consumer/ledger refusal and anti-vacuity tests to execute and pass.
INEFFECTIVE_CONTROLS = {
    "M01": {
        "refusal": "tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts::test_m01_an_absent_settlement_source_mints_nothing",
        "positive": "tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts::test_control_a_complete_observation_mints",
        "reason": "The late substituted authority carries no independently replayable source preimage; candidate_contract rejects the present missing proof. The original zero-mint/zero-prediction assertion and a real minting control both pass. M01P separately fabricates the authority before source capture and must be killed.",
    },
}

# Reviewed witness manifest. A node selector alone is not enough: the failed
# traceback must contain this particular semantic operation. Diagnostic
# counters, refusal wording and unrelated assertions are deliberately absent.
# ``ast.unparse`` in the evidence plugin gives statements a stable spelling.
_WITNESS_ASSERTIONS = {
    "M01": ["self.assertEqual(pending, []"],
    "M01P": ["self.assertEqual(pending, []"],
    "M02": ["self.assertEqual(pending, []"],
    "M03": ["self.assertEqual(pending, []", "self.assertEqual(len(pending), 1"],
    "M04": ["self.assertEqual(pending, []"],
    "M05": ["self.assertEqual(pending, []"],
    "M06": ["self.assertEqual(pending, []"],
    "M07": ["self.assertEqual(pending, []"],
    "M07P": ["self.assertEqual(pending, []"],
    "M08": ["self.assertEqual(bad, []"],
    "M09": ["self.assertFalse(feed.emit_candidate("],
    "M10": ["self.assertTrue(open(ledger.log.path, 'rb').read().startswith(first))", "self.assertEqual(rows, [{'i': i} for i in range(5)])"],
    "M11": ["self.assertEqual(pending, []", "self.assertEqual([s.contract_id for s, _ in pending], ['KX-GOOD'])"],
    "M12": ["self.assertIsNone(self.emit([{'name': bad}])"],
    "M13": ["self.assertIn('expected_resolution_time_utc', candidate['contradictory_fields'])"],
    "M14": ["self.assertFalse(refused"],
    "M15": ["self.assertFalse(ledger.prediction_is_committed('snap-1'))"],
    "M16": ["self.assertEqual(result['appended'], 0)"],
    "M17": ["self.assertEqual(result['appended'], 0)"],
    "M18": ["self.assertEqual(len(lines), 3"],
    "M19": ["errors = validate_record(record)"],
    "M20": ["self.assertEqual(provider.calls, 0"],
    "M21": ["self.assertEqual(provider.calls, 0"],
    "M22": ["self.assertEqual(len(rows), 1"],
    "M23": ["with self.assertRaises(ValueError):"],
    "M24": ["self.assertEqual(binding['source_evidence']['contract_id']", "self.assertEqual(recomputed, binding['record_sha256'])"],
    "M25": ["self.assertLess(elapsed,"],
    "M26": ["self.assertEqual(calls, []", "self.assertEqual(provider.calls, 0", "self.assertFalse("],
    "M27": ["self.assertIsNone(valid_candidate(market)['resolution_source']"],
    "M28": ["self.assertEqual(pending, []", "self.assertIsNone("],
    "M29": ["self.assertLess(elapsed,"],
    "M30": ["self.assertEqual(capacity['records'], 3"],
    "M31": ["with self.assertRaises(RuntimeError):"],
    "M32": ["self.assertEqual(opportunity['prediction_id'], durable"],
    "M33": ["self.assertFalse(result['deferred'])", "self.assertEqual(provider.calls, 0"],
    "M34": ["self.assertIsNotNone(mine.status('snap-theirs')"],
    "M35": ["self.assertTrue(result['deferred'])", "self.assertNotEqual("],
    "M36": ["self.assertEqual(result['appended'], 0"],
    "M37": ["self.assertEqual(result['appended'], 0"],
    "M38": ["self.assertEqual(report['astra']['samples'], 0"],
    "M39": ["self.assertIsNone(ledger.calibration('astra')"],
    "M40": ["with self.assertRaises(ValueError) as caught:"],
}
_WITNESS_ASSERTIONS.update({
    "M26P": ["self.assertFalse(self.store.seen(snapshot.market_snapshot_id))", "self.assertTrue(result['deferred'])"],
    "M41": ["self.assertEqual(self.classify(", "self.assertEqual(probe._classify(proc)[0], 'INCONCLUSIVE_INFRASTRUCTURE')"],
    "M42": ["self.assertIsNone(self.feed._build(candidate))", "self.assertEqual(consumer.pending(), []"],
    "M43": ["self.assertFalse(spool.write(record))"],
    "M44": ["self.assertEqual(result['appended'], 0)", "self.assertFalse(ok, reason)"],
    "M45": ["self.assertEqual(result['appended'], 0)", "self.assertFalse(ok, reason)"],
    "M46": ["self.assertTrue(done.wait(0.75), 'research blocked the engine hook')"],
    "M47": ["self.assertFalse(ok, reason)", "self.assertTrue(result['deferred'])", "self.assertEqual(provider.calls, 0)"],
    "M48": ["self.assertEqual(second_provider.calls, 0)"],
})
_WITNESS_ASSERTIONS.update({
    "M49": ["self.assertEqual(result['appended'], 0)"],
    "M50": ["self.assertEqual(json.loads(target.read_text()).get('cycles'), 9)"],
})
SEMANTIC_WITNESSES = {
    key: [{"node": selector, "assertion": assertion,
           "invariant": description,
           "exceptions": (["RuntimeError"] if key == "M09" else
                          ["OverflowError", "RecursionError", "MemoryError",
                           "ZeroDivisionError"] if key == "M19" else
                          ["KeyError", "AssertionError"] if key == "M24" else
                          ["AssertionError"])}
          for selector in selectors
          for assertion in _WITNESS_ASSERTIONS.get(key, ())]
    for key, (description, _filename, _old, _new, selectors) in MUTATIONS.items()
}

def _semantic_failure(report, witnesses):
    if report.get("when") != "call" or report.get("fixture_failure"):
        return None
    for witness in witnesses:
        if not report.get("nodeid", "").startswith(witness["node"]):
            continue
        if report.get("exception") not in witness.get("exceptions", ["AssertionError"]):
            continue
        for frame in report.get("frames", []):
            if witness["assertion"] in frame.get("statement", ""):
                return {"invariant": witness["invariant"],
                        "nodeid": report["nodeid"], "frame": frame}
    return None


def _classify(proc, evidence=None, witnesses=(), diagnostic=False) -> tuple:
    """Classify structured phases and reviewed semantic assertions.

    A pytest summary is only a display count.  It cannot establish collection,
    execution of a test body, or the meaning of an assertion.  Missing or
    damaged phase evidence consequently never establishes a behavioral kill.
    """
    detail = {"counts": _outcomes(proc.stdout), "semantic_witnesses": []}
    if not isinstance(evidence, dict) or evidence.get("schema") != "astra-mutation-phases-v1":
        return "INCONCLUSIVE_INFRASTRUCTURE", detail
    if evidence.get("session_exit") != proc.returncode:
        return "INCONCLUSIVE_INFRASTRUCTURE", detail
    collection = evidence.get("collection_errors", [])
    if collection:
        text = "\n".join(collection)
        status = "INCONCLUSIVE_IMPORT" if any(token in text for token in (
            "ImportError", "ModuleNotFoundError", "SyntaxError")) else "INCONCLUSIVE_COLLECTION"
        return status, detail
    if not evidence.get("collected"):
        return "INCONCLUSIVE_COLLECTION", detail
    reports = evidence.get("reports", [])
    failures = [r for r in reports if r.get("outcome") == "failed"]
    if any(r.get("fixture_failure") or r.get("when") != "call" for r in failures):
        return "INCONCLUSIVE_SETUP", detail
    if proc.returncode == 0:
        if failures or not any(r.get("when") == "call" and r.get("outcome") == "passed"
                               for r in reports):
            return "INCONCLUSIVE_INFRASTRUCTURE", detail
        return "DIAGNOSTIC_ONLY" if diagnostic else "SURVIVED", detail
    if proc.returncode != 1 or not failures:
        return "INCONCLUSIVE_INFRASTRUCTURE", detail
    semantic = [match for report in failures
                if (match := _semantic_failure(report, witnesses)) is not None]
    if semantic:
        detail["semantic_witnesses"] = semantic
        return "KILLED_BEHAVIORALLY", detail
    if all(r.get("exception") == "AssertionError" for r in failures):
        # Untagged assertions cannot prove a safety invariant.  They remain
        # diagnostic, and an effective mutation with only this evidence fails
        # the gate even though this label is deliberately not a kill.
        return "DIAGNOSTIC_ONLY", detail
    return "INCONCLUSIVE_INFRASTRUCTURE", detail


def _phase_run(root, selectors, phase, timeout=1800):
    receipt = os.path.join(root, f".astra-{phase}-phases.json")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "-p", "tools.astra_mutation_pytest", "--astra-phase-file", receipt,
               *selectors]
    proc = subprocess.run(command, cwd=root, capture_output=True, text=True,
                          timeout=timeout)
    try:
        with open(receipt, encoding="utf-8") as stream:
            evidence = json.load(stream)
    except (OSError, ValueError):
        evidence = None
    return proc, evidence


def _save_phase(directory, key, phase, proc, evidence):
    """Retain complete disposable-test outputs for independent classification."""
    if not directory:
        return
    os.makedirs(directory, exist_ok=True)
    prefix = os.path.join(directory, f"{key}.{phase}")
    for suffix, content in (("stdout.log", proc.stdout or ""),
                            ("stderr.log", proc.stderr or ""),
                            ("phases.json", json.dumps(evidence, indent=2))):
        with open(prefix + "." + suffix, "w", encoding="utf-8") as stream:
            stream.write(content)


def summarize(results):
    behavioral = sum(r["status"] == "KILLED_BEHAVIORALLY" for r in results)
    inconclusive = sum(r["status"].startswith("INCONCLUSIVE_") for r in results)
    not_applied = sum(r["status"] == "NOT_APPLIED" for r in results)
    effective_unresolved = [r for r in results
                            if r.get("effective_safety_mutation", True)
                            and r["status"] != "KILLED_BEHAVIORALLY"]
    return {
        "mode": "SHADOW_ONLY", "broker_authority": False,
        "mutations_run": len(results), "killed": behavioral,
        "behavioural_kills": behavioral,
        "diagnostic_only": sum(r["status"] == "DIAGNOSTIC_ONLY" for r in results),
        "kills_with_setup_errors": 0,
        "inconclusive": inconclusive, "not_applied": not_applied,
        "surviving_effective_safety_mutations": sum(
            r["status"] == "SURVIVED" and r.get("effective_safety_mutation", True)
            for r in results),
        "unresolved_effective_safety_mutations": len(effective_unresolved),
        "survivors": sum(r["status"] == "SURVIVED" for r in results),
        "gate_passed": not effective_unresolved and not inconclusive and not not_applied,
        "results": results,
    }

def run_one(key, verbose=False, evidence_dir=None):
    description, filename, old, new, selectors = MUTATIONS[key]
    result = {"mutation": key, "description": description,
              "effective_safety_mutation": key not in DIAGNOSTIC_MUTATIONS,
              "detecting_tests": selectors}
    workdir = tempfile.mkdtemp(prefix=f"astra-mut-{key}-")
    try:
        root = os.path.join(workdir, "repo")
        _copy_repo(root)
        target = os.path.join(root, filename)
        with open(target, encoding="utf-8") as stream:
            source = stream.read()
        if source.count(old) != 1:
            return dict(result, status="NOT_APPLIED",
                        detail=f"expected exactly one anchor in {filename}; found {source.count(old)}")
        # A broken positive baseline invalidates a mutation experiment.  Both
        # runs execute the same selectors under the same local environment.
        baseline, baseline_evidence = _phase_run(root, selectors, "baseline")
        _save_phase(evidence_dir, key, "baseline", baseline, baseline_evidence)
        baseline_status, baseline_detail = _classify(baseline, baseline_evidence)
        if baseline_status != "SURVIVED":
            status = baseline_status if baseline_status.startswith("INCONCLUSIVE_") else "INCONCLUSIVE_INFRASTRUCTURE"
            return dict(result, status=status,
                        detail="unmutated detecting tests do not pass",
                        baseline=baseline_detail,
                        output=(baseline.stdout + baseline.stderr)[-6000:])
        with open(target, "w", encoding="utf-8") as stream:
            stream.write(source.replace(old, new, 1))
        proc, evidence = _phase_run(root, selectors, "mutated")
        _save_phase(evidence_dir, key, "mutated", proc, evidence)
        status, detail = _classify(proc, evidence, SEMANTIC_WITNESSES.get(key, ()),
                                   diagnostic=key in DIAGNOSTIC_MUTATIONS)
        effectiveness = INEFFECTIVE_CONTROLS.get(key)
        if effectiveness and status == "SURVIVED":
            passed = {row["nodeid"] for row in evidence["reports"]
                      if row.get("when") == "call" and row.get("outcome") == "passed"}
            required = {effectiveness["refusal"], effectiveness["positive"]}
            if required.issubset(passed):
                result["effective_safety_mutation"] = False
                result["effectiveness_evidence"] = effectiveness
            else:
                status = "INCONCLUSIVE_INFRASTRUCTURE"
        return dict(result, status=status, outcomes=detail["counts"],
                    baseline={"returncode": baseline.returncode,
                              "outcomes": baseline_detail["counts"],
                              "collected": baseline_evidence["collected"]},
                    semantic_witnesses=detail["semantic_witnesses"],
                    phase_evidence=evidence,
                    detail=(proc.stdout + proc.stderr)[-6000:] if verbose
                           or status != "KILLED_BEHAVIORALLY" else "reviewed semantic assertion failed")
    except subprocess.TimeoutExpired as exc:
        return dict(result, status="INCONCLUSIVE_INFRASTRUCTURE",
                    detail=f"synthetic test process exceeded {exc.timeout}s")
    except (OSError, ValueError) as exc:
        return dict(result, status="INCONCLUSIVE_INFRASTRUCTURE",
                    detail=f"mutation experiment unavailable: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=None,
                        help="run just these mutation ids")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--evidence-dir", default=None,
                        help="retain complete baseline/mutated logs and phase receipts")
    args = parser.parse_args(argv)
    keys = args.only or sorted(MUTATIONS)
    unknown = sorted(set(keys) - set(MUTATIONS))
    if unknown:
        parser.error(f"unknown mutation IDs: {unknown}")
    summary = summarize([run_one(key, verbose=args.verbose,
                                 evidence_dir=args.evidence_dir) for key in keys])
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for row in summary["results"]:
            print(f"{row['mutation']}  {row['status']:<32} {row['description']}")
        print(f"\n{summary['behavioural_kills']} behavioral kills; "
              f"{summary['diagnostic_only']} diagnostic; "
              f"{summary['inconclusive']} inconclusive; "
              f"{summary['unresolved_effective_safety_mutations']} effective unresolved")
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
