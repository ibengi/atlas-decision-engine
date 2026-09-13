"""LI05 original identity counterexamples and neighboring synthetic cases."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from alpha_identity import (canonical, digest, verify_receipt, qualified_prediction_signal,
                            POLICY_VERSION, REVIEWED_MODEL_MAPPINGS, TransportObservation)
from alpha_providers import OpenAIProvider, ProviderError
from alpha_schema import validate_signal
from alpha_dispatcher import _run_one, DispatchResult
from alpha_meta import ensemble
from alpha_gateway import AlphaGateway
from alpha_ledger import AlphaLedger, LedgerError
from alpha_learning import score_model
from config import CFG
from tests._alpha_identity import evidence_fixture, MAPPINGS, POLICY, MODEL, MODEL_KEY, ENDPOINT


class ProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(CFG, "ALPHA_ENVIRONMENT", "demo"))
        self.snapshot, self.output, self.body, self.response, self.receipt = evidence_fixture()
        self.now = datetime.now(timezone.utc)
        self.provider = OpenAIProvider(model="synthetic-requested")

    def verify(self, receipt=None, **kwargs):
        return verify_receipt(self.receipt if receipt is None else receipt,
                              snapshot=kwargs.pop("snapshot", self.snapshot),
                              environment=kwargs.pop("environment", "demo"), **kwargs)

    def reviewed(self):
        # An explicit synthetic authority mapping is never installed outside
        # this scoped test. Production defaults remain empty.
        self.enterContext(patch("alpha_identity.POLICY_VERSION", POLICY))
        self.enterContext(patch("alpha_identity.REVIEWED_MODEL_MAPPINGS", MAPPINGS))
        self.snapshot, self.output, self.body, self.response, self.receipt = evidence_fixture()

    def signal(self):
        return validate_signal(self.output, self.snapshot, provider="openai",
            model="synthetic-requested", received_at=self.now,
            provider_identity_receipt=self.receipt)

    def opportunity(self, prediction_id="prediction-synthetic", receipt=None, snapshot=None):
        receipt = receipt or self.receipt
        snapshot = snapshot or self.snapshot
        return {"prediction_id": prediction_id, "contract_id": snapshot.contract_id,
            "prediction_time": datetime.now(timezone.utc).isoformat(),
            "market_snapshot_id": snapshot.market_snapshot_id,
            "snapshot": snapshot.as_dict(), "source_binding": {"environment": "demo"},
            "per_model": {receipt["provider"] + "/" + receipt["resolved_model"]: {"p_yes": .6,
                "low":.57,"high":.63,"confidence":.7,"provider":receipt["provider"],
                "provider_identity_receipt": receipt,
                "provider_prediction_binding": {"prediction_id": prediction_id,
                                                  "receipt_sha256": receipt["receipt_sha256"]}}}}

    def rehash(self, receipt):
        receipt["receipt_sha256"] = digest({k:v for k,v in receipt.items() if k != "receipt_sha256"})
        return receipt

    def test_w01_transport_header_retained_outside_generated_json(self):
        from requests.structures import CaseInsensitiveDict
        fake = SimpleNamespace(status_code=200, content=canonical(self.body).encode(),
            url=ENDPOINT, history=[], headers=CaseInsensitiveDict({"X-Request-ID":"req-transport"}))
        calls = []
        def post(url, **kwargs):
            calls.append(kwargs)
            return fake
        session = SimpleNamespace(post=post)
        with patch("requests.Session", return_value=session):
            result = self.provider._post(ENDPOINT, headers={}, payload={}, timeout=1)
        self.assertEqual(result.observation.as_dict()["transport_request_id"], "req-transport")
        self.assertIs(calls[0]["verify"], True)
        self.assertIs(calls[0]["allow_redirects"], False)
        self.assertIs(session.trust_env, False)

    def test_w02_envelope_id_and_model_survive_extraction(self):
        _, usage = self.provider._extract(self.body)
        self.assertEqual(usage["response_id"], self.body["id"])
        self.assertEqual(usage["response_model"], MODEL)

    def test_w03_analysis_retains_configured_and_observed_identity_separately(self):
        with patch.object(self.provider, "configured", return_value=True), \
             patch.object(self.provider, "_call", return_value=self.response):
            output, meta = self.provider.analyze(self.snapshot, 1)
        self.assertEqual(output, self.output)
        self.assertEqual(meta["model"], "synthetic-requested")
        self.assertEqual(meta["resolved_model"], MODEL)
        self.assertEqual(meta["provider_identity_receipt"]["response_id"], self.body["id"])
        self.assertIs(meta["provider_identity_verification"]["qualified"], False)

    def test_w04_generated_text_cannot_change_model_attribution(self):
        signal = validate_signal(self.output, self.snapshot, provider="openai",
                                 model="synthetic-requested", received_at=self.now)
        self.assertTrue(signal.valid)
        self.assertEqual(signal.model, "synthetic-requested")
        self.assertEqual(signal.model_version, "")
        self.assertFalse(signal.provider_identity_qualified)

    def test_w05_dispatch_retains_only_current_builtin_transport_capture(self):
        fake=SimpleNamespace(status_code=200,content=canonical(self.body).encode(),
                             url=ENDPOINT,headers={},history=[])
        session=SimpleNamespace(post=lambda *a,**kw:fake)
        with patch("requests.Session",return_value=session), \
             patch.object(self.provider,"configured",return_value=True), \
             patch.object(self.provider,"_api_key",return_value="synthetic-key"):
            signal=_run_one(self.provider,self.snapshot,1,lambda:self.now)
        self.assertTrue(signal.valid,signal.rejected_reason)
        self.assertEqual(signal.as_dict()["provider_identity_receipt"]["response_id"],self.body["id"])
        with patch.object(self.provider,"analyze",return_value=(self.output,
                {"cost":{},"error":None,"provider_identity_receipt":self.receipt})):
            signal=_run_one(self.provider,self.snapshot,1,lambda:self.now)
        self.assertFalse(signal.valid)
        self.assertEqual(signal.rejected_reason,"provider_identity_untrusted_capture")

    def test_w06_generated_or_configured_astra_label_never_qualifies(self):
        self.assertEqual(score_model([{"per_model":{"self-astra-label":{"p_yes":.6}},
                                      "actual_outcome":1}], "astra")["samples"], 0)
        self.assertFalse(self.verify()["qualified"])
        self.assertEqual(REVIEWED_MODEL_MAPPINGS, frozenset())

    def test_w07_wrong_snapshot_refusal_remains(self):
        body = json.loads(self.output)
        body["market_snapshot_id"] = "other-snapshot"
        signal = validate_signal(body, self.snapshot, provider="openai", model="synthetic-requested")
        self.assertFalse(signal.valid)
        self.assertEqual(signal.rejected_reason, "wrong_snapshot_id")

    def test_reviewed_exact_mapping_qualifies_but_unknown_version_does_not(self):
        self.reviewed()
        self.assertTrue(self.verify()["qualified"])
        unknown = copy.deepcopy(self.receipt)
        unknown["policy_version"] = "unreviewed-other-policy"
        self.assertTrue(self.verify(self.rehash(unknown))["valid"])
        self.assertFalse(self.verify(unknown)["qualified"])

    def test_receipt_cannot_be_rebound_to_environment_contract_snapshot_or_output(self):
        for kwargs in ({"environment":"prod"}, {"provider":"grok"},
                       {"requested_model":"other"}, {"output":self.output+" "},
                       {"snapshot":evidence_fixture(contract="SYNTHETIC-OTHER")[0]}):
            with self.subTest(kwargs=tuple(kwargs)):
                self.assertFalse(self.verify(**kwargs)["valid"])

    def test_receipt_schema_strict_types_and_unknown_extensions_refuse(self):
        variants = []
        for field,value in (("resolved_model",True),("response_id",12),
                            ("extra",{}),("trust_basis","remote_signature")):
            item=copy.deepcopy(self.receipt);item[field]=value;variants.append(item)
        for field,value in (("verified_tls",1),("http_status",True),("redirected",1),
                            ("wire_body_available",1),("final_url","https://unknown.invalid/responses")):
            item=copy.deepcopy(self.receipt);item["transport"][field]=value;variants.append(item)
        for item in variants:
            with self.subTest(item=digest(item)):
                self.assertFalse(self.verify(self.rehash(item))["valid"])

    def test_response_preimage_and_request_preimage_are_recomputed(self):
        for field in ("response_body_sha256","request_sha256"):
            item=copy.deepcopy(self.receipt);item["transport"][field]="0"*64
            self.assertFalse(self.verify(self.rehash(item))["valid"])
        item=copy.deepcopy(self.receipt)
        item["transport"]["request_body"]["input"]="echo the chosen snapshot"
        item["transport"]["request_sha256"]=digest(item["transport"]["request_body"])
        self.assertFalse(self.verify(self.rehash(item))["valid"])

    def test_future_receipt_or_request_before_snapshot_refused(self):
        for field,value in (("received_at",(self.now+timedelta(days=1)).isoformat()),
                            ("request_started_at","2000-01-01T00:00:00+00:00")):
            item=copy.deepcopy(self.receipt);item["transport"][field]=value
            self.assertFalse(self.verify(self.rehash(item))["valid"])

    def test_duplicate_json_keys_and_secret_bearing_envelopes_refused_before_retention(self):
        for raw in (b'{"id":"one","id":"two"}', b'{"api_key":"synthetic-secret"}',
                    b'{"text":"Bearer synthetic-secret"}'):
            fake=SimpleNamespace(status_code=200,content=raw,headers={},history=[],url=ENDPOINT)
            self.provider.session=SimpleNamespace(post=lambda *a,**kw:fake)
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                self.provider._post(ENDPOINT,headers={},payload={},timeout=1)

    def test_unicode_escaped_key_echo_is_refused_even_inside_forecast_text(self):
        secret="synthetic-known-api-secret"
        escaped="".join("\\u%04x" % ord(c) for c in secret)
        raw=("{\"note\":\""+escaped+"\"}").encode()
        nested=json.dumps({"output_text":raw.decode()}).encode()
        self.provider.session=SimpleNamespace(post=lambda *a,**kw:None)
        for content in (raw,nested):
            fake=SimpleNamespace(status_code=200,content=content,headers={},history=[],url=ENDPOINT)
            self.provider.session.post=lambda *a,**kw:fake
            with patch.dict("os.environ",{"OPENAI_API_KEY":secret}), self.assertRaises(ProviderError):
                self.provider._post(ENDPOINT,headers={},payload={},timeout=1)

    def test_injected_transport_and_redirect_do_not_prove_verified_tls(self):
        fake=SimpleNamespace(status_code=200,content=canonical(self.body).encode(),headers={},
                             history=[],url=ENDPOINT)
        self.provider.session=SimpleNamespace(post=lambda *a,**kw:fake)
        response=self.provider._post(ENDPOINT,headers={},payload={},timeout=1)
        self.assertIs(response.observation.as_dict()["verified_tls"],False)
        fake.status_code=302
        with self.assertRaises(ProviderError):
            self.provider._post(ENDPOINT,headers={},payload={},timeout=1)

    def test_gateway_receipt_binding_does_not_mutate_signal(self):
        signal=self.signal();before=signal.as_dict()
        result=DispatchResult([signal],self.now,self.now)
        meta=ensemble([signal],self.snapshot,self.now)
        gateway=object.__new__(AlphaGateway)
        row=gateway._record(self.snapshot,result,meta,{},None,"NO_EDGE",None,0.,self.now,
                            source_binding={"environment":"demo"})
        self.assertEqual(signal.as_dict(),before)
        self.assertEqual(row["per_model"][MODEL_KEY]["provider_prediction_binding"]["prediction_id"],row["prediction_id"])

    def test_committed_receipt_reconstructs_after_restart_without_rewrite(self):
        self.reviewed()
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"predictions.jsonl")
            ledger=AlphaLedger(path=path,cost_path=str(Path(directory)/"cost.jsonl"))
            row=ledger.record_prediction(self.opportunity())
            before=Path(path).read_bytes()
            restarted=AlphaLedger(path=path,cost_path=str(Path(directory)/"cost.jsonl"))
            recovered=restarted.find_prediction(row["prediction_id"])
            self.assertTrue(qualified_prediction_signal(recovered,MODEL_KEY,recovered["per_model"][MODEL_KEY]))
            self.assertEqual(Path(path).read_bytes(),before)

    def test_duplicate_response_across_requests_refused_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"predictions.jsonl")
            ledger=AlphaLedger(path=path,cost_path=str(Path(directory)/"cost.jsonl"))
            ledger.record_prediction(self.opportunity())
            before=Path(path).read_bytes()
            snapshot,_,_,_,receipt=evidence_fixture(contract="SYNTHETIC-OTHER",request="different-request")
            restarted=AlphaLedger(path=path,cost_path=str(Path(directory)/"cost.jsonl"))
            with self.assertRaisesRegex(LedgerError,"reused"):
                restarted.record_prediction(self.opportunity("prediction-other",receipt,snapshot))
            self.assertEqual(Path(path).read_bytes(),before)

    def test_six_fsync_failures_never_authorize_committed_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"predictions.jsonl")
            ledger=AlphaLedger(path=path,cost_path=str(Path(directory)/"cost.jsonl"))
            ledger.record_prediction(self.opportunity())
            before=Path(path).read_bytes()
            from alpha_ledger import analysis_identity
            for _ in range(6):
                restarted=AlphaLedger(path=path,cost_path=str(Path(directory)/"cost.jsonl"))
                with patch("durable_append.os.fsync",side_effect=OSError("synthetic fsync failure")):
                    with self.assertRaises(LedgerError):
                        restarted.committed_prediction(self.snapshot.market_snapshot_id)
            self.assertEqual(Path(path).read_bytes(),before)
            self.assertIsNotNone(ledger.committed_prediction(self.snapshot.market_snapshot_id))

    def test_copied_prediction_binding_and_probability_do_not_qualify(self):
        self.reviewed()
        row=self.opportunity()
        self.assertTrue(qualified_prediction_signal(row,MODEL_KEY,row["per_model"][MODEL_KEY]))
        for field in ("prediction_id","p_yes","low","high","confidence","provider"):
            other=copy.deepcopy(row)
            if field=="prediction_id":other[field]="different-prediction"
            else:other["per_model"][MODEL_KEY][field]="grok" if field=="provider" else .99
            self.assertFalse(qualified_prediction_signal(other,MODEL_KEY,other["per_model"][MODEL_KEY]))

    def test_configured_astra_report_selector_cannot_bypass_qualification(self):
        from alpha_learning import learning_report
        class LegacyLedger:
            def qualified_resolved(inner):
                return [{"prediction_id":"old", "actual_outcome":1,
                         "per_model":{MODEL:{"p_yes":.6}}}]
            def unqualified_resolved(inner):
                return []
        for selector in ("astra", "a_stra", MODEL, "gpt", ""):
            with self.subTest(selector=selector):
                report=learning_report(LegacyLedger(),astra_selector=selector)
                self.assertEqual(report["astra"]["samples"],0)
                self.assertEqual(report["memory"],[])

    def test_url_credentials_or_query_auth_never_leave_or_enter_receipt(self):
        calls=[]
        self.provider.session=SimpleNamespace(post=lambda *a,**kw:calls.append(a))
        for endpoint in ("https://api.openai.com/v1/responses?api_key=synthetic",
                         "https://synthetic-secret@api.openai.com/v1/responses",
                         "http://api.openai.com/v1/responses"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ProviderError):
                self.provider._post(endpoint,headers={},payload={},timeout=1)
        self.assertEqual(calls,[])

    def test_duplicate_generic_labels_fail_closed_without_probability_inflation(self):
        signal=validate_signal(self.output,self.snapshot,provider="openai",model="same-label",received_at=self.now)
        result=ensemble([signal,replace(signal,provider="grok")],self.snapshot,self.now)
        self.assertIsNone(result["p_meta"])
        self.assertEqual(result["models"],0)
        self.assertEqual(result["per_model"],{})

    def test_custom_provider_cannot_self_authenticate_receipt_metadata(self):
        self.reviewed()
        receipt=copy.deepcopy(self.receipt)
        class EchoProvider:
            name="openai"
            model="synthetic-requested"
            def analyze(inner,*args):
                return self.output,{"cost":{},"error":None,"provider_identity_receipt":receipt}
        signal=_run_one(EchoProvider(),self.snapshot,1,lambda:self.now)
        self.assertFalse(signal.valid)
        self.assertFalse(signal.provider_identity_qualified)

    def test_subclass_cannot_self_authenticate_receipt_metadata(self):
        self.reviewed()
        class EchoSubclass(OpenAIProvider):
            def analyze(inner,*args):
                return self.output,{"cost":{},"error":None,"provider_identity_receipt":self.receipt}
        signal=_run_one(EchoSubclass(model="synthetic-requested"),self.snapshot,1,lambda:self.now)
        self.assertFalse(signal.valid)

    def test_qualified_settlement_and_provider_identity_jointly_enter_astra_learning(self):
        self.reviewed()
        from tests._settlement import qualified_fixture
        from alpha_resolution_ingest import ingest_settlements
        from alpha_learning import learning_report
        _,snapshot,prediction,settlement=qualified_fixture()
        _,_,_,_,receipt=evidence_fixture(snapshot=snapshot)
        attributed=self.opportunity(prediction["prediction_id"],receipt,snapshot)
        prediction["per_model"]=attributed["per_model"]
        with tempfile.TemporaryDirectory() as directory:
            ledger=AlphaLedger(path=str(Path(directory)/"predictions.jsonl"),cost_path=str(Path(directory)/"cost.jsonl"))
            ledger.record_prediction(prediction)
            self.assertEqual(ingest_settlements(ledger,[settlement],trusted_sources=[settlement["source"]])["appended"],1)
            self.assertEqual(learning_report(ledger)["astra"]["samples"],1)

    def test_malformed_forecast_envelope_returns_structured_refusal(self):
        import base64,hashlib
        for changes in ({"output_text":None},{"output_text":""},
                        {"output_text":None,"output":[{}]}):
            receipt=copy.deepcopy(self.receipt)
            body=dict(self.body,**changes)
            raw=canonical(body).encode()
            receipt["transport"]["response_body_b64"]=base64.b64encode(raw).decode()
            receipt["transport"]["response_body_sha256"]=hashlib.sha256(raw).hexdigest()
            verdict=self.verify(self.rehash(receipt))
            self.assertFalse(verdict["valid"])
            self.assertFalse(verdict["qualified"])

    def test_legacy_namespace_label_cannot_supply_earned_calibration(self):
        ledger=object.__new__(AlphaLedger)
        row={"actual_outcome":1,"per_model":{MODEL_KEY:{"p_yes":.9}}}
        with patch.object(ledger,"qualified_resolved",return_value=[row]):
            self.assertIsNone(ledger.calibration(MODEL_KEY))

    def test_two_provider_envelopes_with_same_model_name_remain_distinguishable(self):
        first=self.signal()
        receipt=copy.deepcopy(self.receipt)
        receipt["provider"]="grok"
        receipt["transport"]["endpoint"]="https://api.x.ai/v1/responses"
        receipt["transport"]["final_url"]="https://api.x.ai/v1/responses"
        self.rehash(receipt)
        second=validate_signal(self.output,self.snapshot,provider="grok",model="synthetic-requested",
                               received_at=self.now,provider_identity_receipt=receipt)
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        result=ensemble([first,second],self.snapshot,self.now)
        self.assertEqual(result["p_meta"],.6)
        self.assertEqual(len(result["per_model"]),2)
        self.assertEqual(set(result["per_model"]),{"openai/"+MODEL,"grok/"+MODEL})

    def test_boolean_forecast_scalar_cannot_match_numeric_authenticated_value(self):
        from alpha_identity import forecast_matches
        self.assertFalse(forecast_matches({"p_yes":True},{"p_yes":1.0}))
        self.assertFalse(forecast_matches({"p_yes":False},{"p_yes":0.0}))
        self.assertTrue(forecast_matches({"p_yes":1},{"p_yes":1.0}))

    def test_gemini_remains_valid_generic_shadow_research_without_astra_identity(self):
        from alpha_providers import GeminiProvider
        provider=GeminiProvider(model="synthetic-gemini")
        body={"responseId":"gemini-synthetic-id","modelVersion":"synthetic-gemini-version",
              "candidates":[{"content":{"parts":[{"text":self.output}]}}],
              "usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1}}
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/synthetic-gemini:generateContent"
        fake=SimpleNamespace(status_code=200,content=canonical(body).encode(),headers={},history=[],url=endpoint)
        with patch("requests.Session",return_value=SimpleNamespace(post=lambda *a,**kw:fake)), \
             patch.object(provider,"configured",return_value=True), \
             patch.object(provider,"_api_key",return_value="synthetic-key"):
            signal=_run_one(provider,self.snapshot,1,lambda:self.now)
        self.assertTrue(signal.valid,signal.rejected_reason)
        self.assertFalse(signal.provider_identity_qualified)
        self.assertIsNone(signal.as_dict()["provider_identity_receipt"])
        self.assertEqual(signal.model,"synthetic-gemini")

    def test_qualified_provider_identity_cannot_bypass_unqualified_settlement(self):
        self.reviewed()
        from tests._settlement import qualified_fixture
        from alpha_learning import learning_report
        _,snapshot,prediction,_=qualified_fixture()
        _,_,_,_,receipt=evidence_fixture(snapshot=snapshot)
        prediction["per_model"]=self.opportunity(prediction["prediction_id"],receipt,snapshot)["per_model"]
        with tempfile.TemporaryDirectory() as directory:
            ledger=AlphaLedger(path=str(Path(directory)/"predictions.jsonl"),cost_path=str(Path(directory)/"cost.jsonl"))
            ledger.record_prediction(prediction)
            ledger.resolve(prediction["prediction_id"],1,source="unqualified synthetic spreadsheet")
            self.assertEqual(len(ledger.resolved()),1)
            self.assertEqual(ledger.qualified_resolved(),[])
            report=learning_report(ledger)
            self.assertEqual(report['astra']['samples'], 0)

    def test_prediction_before_provider_receipt_or_in_future_cannot_qualify(self):
        self.reviewed()
        row=self.opportunity()
        self.assertTrue(qualified_prediction_signal(row,MODEL_KEY,row["per_model"][MODEL_KEY]))
        for when in ((self.snapshot.snapshot_time-timedelta(seconds=1)).isoformat(),
                     (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),None):
            row["prediction_time"]=when
            self.assertFalse(qualified_prediction_signal(row,MODEL_KEY,row["per_model"][MODEL_KEY]))

    def test_same_second_receipt_binding_preserves_prediction_precision(self):
        signal=self.signal()
        result=DispatchResult([signal],self.now,self.now)
        meta=ensemble([signal],self.snapshot,self.now)
        gateway=object.__new__(AlphaGateway)
        now=datetime.now(timezone.utc)
        row=gateway._record(self.snapshot,result,meta,{},None,"NO_EDGE",None,0.,now,
                            source_binding={"environment":"demo"})
        self.assertEqual(row["prediction_time"],now.isoformat())
        self.assertIn(".",row["prediction_time"])

    def test_historical_label_only_rows_remain_unchanged_and_unqualified(self):
        row={"prediction_id":"legacy", "per_model":{MODEL:{"p_yes":.6}},"actual_outcome":1}
        before=canonical(row)
        self.assertEqual(score_model([row],"astra")["samples"],0)
        self.assertEqual(canonical(row),before)


if __name__ == "__main__":
    unittest.main()
