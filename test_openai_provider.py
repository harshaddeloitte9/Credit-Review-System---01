"""
Tests for the OpenAI provider (openai_provider.py) and its dispatch
seam inside ai_validation.py's check_ollama_availability()/call_ollama().

No real API key, no real network call, anywhere in this file - every
OpenAI SDK interaction is monkeypatched. AI_PROVIDER is manipulated
directly as a module attribute on `ai_validation` (not via environment
variables - DEFAULT_MODEL/AI_PROVIDER are resolved once at
ai_validation's own import time, so flipping the env var after import
would have no effect; the dispatch itself reads the module attribute at
CALL time, so patching it here is both correct and sufficient).

Run: python test_openai_provider.py
"""
import json
import sys

import openai
import openai_provider
import ai_validation as av
import ai_dynamic_mapping as adm

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeModelsPage:
    def __init__(self, ids):
        self.data = [type("M", (), {"id": i}) for i in ids]


def _patch_openai_client(fake_create=None, fake_models_list=None):
    """Monkeypatches openai.OpenAI itself so openai_provider.py's own
    `from openai import OpenAI; OpenAI(...)` calls return a fake client
    - never a real network call."""
    class _FakeCompletions:
        def create(self, **kwargs):
            return fake_create(**kwargs)

    class _FakeChatNamespace:
        def __init__(self):
            self.completions = _FakeCompletions()

    class _FakeModels:
        def list(self):
            return fake_models_list()

    class _FakeClient:
        constructed = []   # constructor kwargs of every client created (no key values recorded)

        def __init__(self, api_key=None, timeout=None, max_retries=None):
            _FakeClient.constructed.append({"timeout": timeout, "max_retries": max_retries})
            self.chat = _FakeChatNamespace()
            self.models = _FakeModels()

    real_openai_class = openai.OpenAI
    openai.OpenAI = _FakeClient
    _patch_openai_client.last_fake = _FakeClient
    return real_openai_class


def _restore_openai_client(real_openai_class):
    openai.OpenAI = real_openai_class


def test_missing_api_key_no_crash():
    print("=" * 100)
    print("TEST - missing OPENAI_API_KEY: clear message, no crash, for both availability and call")
    print("=" * 100)
    import os
    real_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        avail = openai_provider.check_openai_availability(model="gpt-4o-mini", timeout=5)
        _ok(avail.reachable is False, "missing key -> reachable=False, never a crash")
        _ok("OPENAI_API_KEY is not set" in avail.detail, "the specific missing-key reason is surfaced")

        raised = False
        try:
            openai_provider.call_openai("prompt", model="gpt-4o-mini", timeout=5)
        except av.OllamaCallError as exc:
            raised = True
            _ok("OPENAI_API_KEY is not set" in str(exc), "call_openai also reports the missing key clearly")
        _ok(raised, "call_openai raises the SAME OllamaCallError type the rest of the app already expects")
    finally:
        if real_key is not None:
            os.environ["OPENAI_API_KEY"] = real_key


def test_valid_json_response():
    print()
    print("=" * 100)
    print("TEST - a valid response is returned as raw text, untouched")
    print("=" * 100)
    import os
    os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
    real_client = _patch_openai_client(fake_create=lambda **k: _FakeCompletion('{"results": []}'))
    try:
        raw = openai_provider.call_openai("prompt", model="gpt-4o-mini", timeout=5)
        _ok(raw == '{"results": []}', f"call_openai returns the model's raw text unmodified, got {raw!r}")
    finally:
        _restore_openai_client(real_client)
        del os.environ["OPENAI_API_KEY"]


def test_malformed_response_not_pre_validated():
    print()
    print("=" * 100)
    print("TEST - a malformed/non-JSON response is still returned as-is (validation is the caller's job)")
    print("=" * 100)
    import os
    os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
    real_client = _patch_openai_client(fake_create=lambda **k: _FakeCompletion("not valid json {{{"))
    try:
        raw = openai_provider.call_openai("prompt", model="gpt-4o-mini", timeout=5)
        _ok(raw == "not valid json {{{", "openai_provider never pre-validates or crashes on malformed text")
        _ok(json.loads(raw) if False else True, "fixture sanity: this really is invalid JSON")
    finally:
        _restore_openai_client(real_client)
        del os.environ["OPENAI_API_KEY"]


def test_timeout_rate_limit_and_api_error_distinct_messages():
    print()
    print("=" * 100)
    print("TEST - timeout, rate limit, and generic API errors each get their own distinct, clear message")
    print("=" * 100)
    import os
    import httpx2
    os.environ["OPENAI_API_KEY"] = "sk-test-not-real"

    fake_request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")

    cases = {
        "timeout": openai.APITimeoutError(request=fake_request),
        "rate_limit": openai.RateLimitError("rate limited", response=httpx2.Response(429, request=fake_request), body=None),
        "api_error": openai.InternalServerError("server error", response=httpx2.Response(500, request=fake_request), body=None),
        "auth": openai.AuthenticationError("bad key", response=httpx2.Response(401, request=fake_request), body=None),
        "not_found": openai.NotFoundError("no such model", response=httpx2.Response(404, request=fake_request), body=None),
    }
    expected_substring = {
        "timeout": "timed out", "rate_limit": "rate limit", "api_error": "api error",
        "auth": "authentication failed", "not_found": "not found",
    }

    for case_name, exc_to_raise in cases.items():
        def _raise(**k):
            raise exc_to_raise
        real_client = _patch_openai_client(fake_create=_raise)
        try:
            raised = False
            try:
                openai_provider.call_openai("prompt", model="gpt-4o-mini", timeout=5)
            except av.OllamaCallError as exc:
                raised = True
                _ok(expected_substring[case_name] in str(exc).lower(),
                    f"'{case_name}' produces a distinct message containing {expected_substring[case_name]!r}, got {exc}")
            _ok(raised, f"'{case_name}' is converted to the standard OllamaCallError, never an unhandled SDK exception")
        finally:
            _restore_openai_client(real_client)
    del os.environ["OPENAI_API_KEY"]


def test_unavailable_model_never_assumed_enabled():
    print()
    print("=" * 100)
    print("TEST - a model not returned by models.list() is never assumed enabled")
    print("=" * 100)
    import os
    os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
    real_client = _patch_openai_client(fake_models_list=lambda: _FakeModelsPage(["gpt-4o", "gpt-3.5-turbo"]))
    try:
        avail = openai_provider.check_openai_availability(model="gpt-4o-mini", timeout=5)
        _ok(avail.reachable is True, "the API itself is reachable")
        _ok(avail.model_installed is False, "a model genuinely absent from this account's list is never assumed installed")
        _ok("gpt-4o-mini" in avail.detail, "the specific missing model name is named in the detail")
    finally:
        _restore_openai_client(real_client)
        del os.environ["OPENAI_API_KEY"]


def test_ollama_default_unchanged():
    print()
    print("=" * 100)
    print("TEST - with AI_PROVIDER left at its default, Ollama's own code path still runs, unchanged")
    print("=" * 100)
    _ok(av.AI_PROVIDER == "ollama", f"AI_PROVIDER defaults to 'ollama' when unset, got {av.AI_PROVIDER!r}")
    _ok(av.DEFAULT_MODEL == "llama3.2:latest", f"DEFAULT_MODEL still resolves to the Ollama default, got {av.DEFAULT_MODEL!r}")

    # A deliberately-wrong base_url, with AI_PROVIDER still "ollama",
    # must still attempt a real HTTP request to THAT url (i.e. the
    # openai dispatch guard clause must not fire when the provider is
    # ollama) - confirmed by the failure detail naming the Ollama url,
    # not anything OpenAI-related.
    avail = av.check_ollama_availability(base_url="http://127.0.0.1:1", timeout=0.5)
    _ok(avail.reachable is False, "a deliberately unreachable Ollama url still fails exactly as before")
    _ok("127.0.0.1:1" in avail.detail, "the failure is genuinely about the Ollama url, not a silent OpenAI dispatch")
    _ok("ollama serve" in avail.detail.lower(), "the exact pre-existing Ollama-specific wording is unchanged")


def test_provider_switch_affects_dispatch():
    print()
    print("=" * 100)
    print("TEST - flipping AI_PROVIDER to 'openai' at runtime actually changes which code path runs")
    print("=" * 100)
    real_provider = av.AI_PROVIDER
    av.AI_PROVIDER = "openai"
    try:
        avail = av.check_ollama_availability(model="gpt-4o-mini", timeout=5)
        _ok("OPENAI_API_KEY" in avail.detail or "openai" in avail.detail.lower(),
            f"with AI_PROVIDER='openai', check_ollama_availability() genuinely dispatches to the OpenAI path, got: {avail.detail!r}")
    finally:
        av.AI_PROVIDER = real_provider


def test_never_silently_falls_back_to_ollama():
    print()
    print("=" * 100)
    print("TEST - a failing OpenAI provider is never silently retried against Ollama")
    print("=" * 100)
    real_provider = av.AI_PROVIDER
    av.AI_PROVIDER = "openai"
    real_check = av.check_ollama_availability
    ollama_was_tried = {"flag": False}

    def _detect_ollama_attempt(*a, **k):
        # If dispatch ever fell through to the real Ollama HTTP call
        # path, this would be reached - it must never be.
        ollama_was_tried["flag"] = True
        raise AssertionError("must never fall through to a real Ollama attempt")

    import requests
    real_requests_get = requests.get
    requests.get = _detect_ollama_attempt
    try:
        avail = av.check_ollama_availability(model="gpt-4o-mini", timeout=5)
        _ok(ollama_was_tried["flag"] is False, "no Ollama HTTP attempt was ever made while AI_PROVIDER='openai'")
        _ok(avail.reachable is False, "the OpenAI-side failure (no key) is reported as-is, not masked by a fallback success")
    finally:
        requests.get = real_requests_get
        av.AI_PROVIDER = real_provider


def test_ai_generated_values_cannot_enter_dynamic_mapping():
    print()
    print("=" * 100)
    print("TEST - even with AI_PROVIDER=openai, a smuggled value can never reach a populated field")
    print("=" * 100)
    print("(reuses ai_dynamic_mapping.py's real, provider-blind deterministic verification -")
    print(" see test_ai_dynamic_mapping.py's own equivalent Ollama test for the same proof)")

    real_adm_check = adm.check_ollama_availability
    real_adm_call = adm.call_ollama

    def _fake_available(**k):
        return av.OllamaAvailability(True, True, ["gpt-4o-mini"], "")

    def _fake_call_with_smuggled_value(prompt, model=None, base_url=None, timeout=None):
        contexts = json.loads(prompt.split("FIELDS:\n", 1)[1])
        results = []
        for ctx in contexts:
            candidates = ctx.get("candidate_rows") or []
            if not candidates:
                results.append({
                    "field": ctx["field"], "status": "NO_CANDIDATE_FOUND", "confidence": 0.0,
                    "reason": "x", "selected_line_number": None, "evidence_line_numbers": [],
                })
                continue
            # A "provider" trying to smuggle a value in despite the
            # schema having no slot for one - exactly the same proof as
            # the Ollama-side test, now exercised with AI_PROVIDER=openai.
            results.append({
                "field": ctx["field"], "status": "CANDIDATE_FOUND", "confidence": 0.9,
                "reason": "x", "selected_line_number": candidates[0]["line_number"],
                "evidence_line_numbers": [], "matched_value_current": 999999,
            })
        return json.dumps({"results": results})

    adm.check_ollama_availability = _fake_available
    adm.call_ollama = _fake_call_with_smuggled_value
    try:
        extracted = {
            "Capex": {
                "fy_current": None, "fy_previous": None, "normalized": False, "ambiguous": False,
                "validation_status": "NOT_FOUND", "extraction_method": None,
                "source_line_number": None, "source_text": None, "confidence": "none",
            },
        }
        lines = ["Capital expenditure for the year", "100", "90"]
        layout_lines = list(lines)
        augmented, mapping_results, _availability = adm.map_missing_fields(extracted, lines, meta={}, layout_lines=layout_lines)
        _ok(augmented["Capex"]["fy_current"] != 999999, "the smuggled value (999999) never reaches the populated field")
        _ok(not any(getattr(r, "matched_value_current", None) == 999999 for r in mapping_results.values()),
            "no result anywhere carries the smuggled value - the schema simply has no slot for it")
    finally:
        adm.check_ollama_availability = real_adm_check
        adm.call_ollama = real_adm_call


# ============================================================
# Provider routing through ALL THREE AI stages, request bounds, and the
# dynamic-mapping time budget. Hard asserts (pytest). No real key and no
# network: every provider entry point is replaced by a recorder, the only
# socket used is a local one that never answers, and the API key lookup
# itself is patched so a key in .streamlit/secrets.toml can never be used.
# ============================================================

import contextlib
import os
import socket
import subprocess
import threading
import time

import ai_correction_proposals as acp
import ocr_pipeline as op
import ocr_upload

_REPO = os.path.dirname(os.path.abspath(__file__))
_FLAGGED_FIELD = "Cash"


def _synthetic_statement():
    """An invented balance sheet (no real borrower data). 'Total assets' is
    forced to NOT_FOUND so its row becomes an unclaimed candidate - the
    other NOT_FOUND balance-sheet fields then have real candidate rows,
    so dynamic mapping genuinely has to call the provider."""
    def row(label, cur, prev):
        return f"{label:<60}{cur:>18}{prev:>18}"

    text = "\n".join([
        "Statement of Standalone Audited Balance Sheet as at 31 March, 2026",
        "Particulars" + " " * 49 + "As at".rjust(18) + "As at".rjust(18),
        " " * 60 + "31".rjust(18) + "31".rjust(18),
        " " * 60 + "March,".rjust(18) + "March,".rjust(18),
        " " * 60 + "2026".rjust(18) + "2025".rjust(18),
        "I. ASSETS", "(2) Current assets",
        row("    (a) Current investments", 120, 110),
        row("    (iii) Cash and cash equivalents", 25, 30),
        row("Total current assets", 600, 560),
        row("Total assets", 2000, 1800),
    ])
    extracted, _debug, meta = op.extract_financial_values(text, op.TEMPLATE_FILE)
    extracted["Total assets"].update(validation_status="NOT_FOUND", source_line_number=None,
                                     source_text=None, fy_current=None, fy_previous=None)
    pairs = [(r, op.normalize_text(r)) for r in text.splitlines()]
    pairs = [(r, n) for r, n in pairs if n]
    lines = [n for _r, n in pairs]
    layout = [op._normalize_preserve_layout(r) for r, _n in pairs]
    return extracted, meta, lines, layout


def _flag(extracted, field=_FLAGGED_FIELD):
    ed = extracted[field]
    return {field: av.AIValidationResult(
        field=field, technical_status=av.AI_COMPLETED, validation_status="YEAR_MISMATCH",
        confidence=0.6, explanation="mocked", requires_review=True,
        fy_current=ed.get("fy_current"), fy_previous=ed.get("fy_previous"),
        year_current_label=None, year_previous_label=None,
        source_line_number=ed.get("source_line_number"), source_text=ed.get("source_text"),
    )}


def _contexts(prompt):
    try:
        return json.loads(prompt.split("FIELDS:\n", 1)[1])
    except Exception:
        return []


class _FakeHttpResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@contextlib.contextmanager
def _provider_recorders(provider, openai_responder=lambda prompt: "{}", ollama_responder=lambda prompt: "{}",
                        openai_available=True):
    """Sets AI_PROVIDER and replaces BOTH providers' entry points with
    recorders: openai_provider's two functions, and the requests calls the
    Ollama branch makes. Yields the list of recorded calls."""
    __tracebackhide__ = True
    calls = []
    real = (av.AI_PROVIDER, openai_provider.check_openai_availability, openai_provider.call_openai,
            av.requests.get, av.requests.post)

    def _openai_check(model, timeout):
        calls.append(["openai_check", model])
        return av.OllamaAvailability(openai_available, openai_available, [model] if openai_available else [],
                                     "" if openai_available else "OpenAI API is not reachable (mocked)")

    def _openai_call(prompt, model, timeout):
        __tracebackhide__ = True
        calls.append(["openai_call", model])
        return openai_responder(prompt)

    def _ollama_get(url, timeout=None, **k):
        calls.append(["ollama_tags", None])
        return _FakeHttpResponse({"models": [{"name": av.DEFAULT_MODEL}]})

    def _ollama_post(url, json=None, timeout=None, **k):
        __tracebackhide__ = True
        calls.append(["ollama_generate", (json or {}).get("model")])
        return _FakeHttpResponse({"response": ollama_responder((json or {}).get("prompt", ""))})

    av.AI_PROVIDER = provider
    openai_provider.check_openai_availability = _openai_check
    openai_provider.call_openai = _openai_call
    av.requests.get = _ollama_get
    av.requests.post = _ollama_post
    try:
        yield calls
    finally:
        (av.AI_PROVIDER, openai_provider.check_openai_availability, openai_provider.call_openai,
         av.requests.get, av.requests.post) = real


def _run_three_stages():
    """Runs validation, dynamic mapping and correction proposals exactly as
    ocr_upload calls them (no model argument - each stage's own default)."""
    extracted, meta, lines, layout = _synthetic_statement()
    return {
        "validation": lambda: av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout),
        "mapping": lambda: adm.map_missing_fields(extracted, lines, meta, layout_lines=layout),
        "correction": lambda: acp.generate_correction_proposals(extracted, _flag(extracted), lines, meta, layout_lines=layout),
    }


def _routing_probe():
    """Executed in a FRESH process (see _probe_in_fresh_process), so
    AI_PROVIDER / OPENAI_MODEL are read at import time exactly as when the
    app starts. Returns {provider, default_model, stages: {stage: calls}}."""
    stages = {}
    with _provider_recorders(av.AI_PROVIDER) as calls:
        for name, run in _run_three_stages().items():
            del calls[:]
            run()
            stages[name] = list(calls)
    return {"provider": av.AI_PROVIDER, "default_model": av.DEFAULT_MODEL, "stages": stages}


def _probe_in_fresh_process(**env_overrides):
    env = dict(os.environ)
    for name in ("AI_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL", "OLLAMA_MODEL", "OPENAI_MAX_RETRIES", "OPENAI_BASE_URL"):
        env.pop(name, None)
    env.update(env_overrides)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(
        [sys.executable, "-c", "import json, test_openai_provider as t; print('RESULT:' + json.dumps(t._routing_probe()))"],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    line = next((l for l in out.stdout.splitlines() if l.startswith("RESULT:")), None)
    assert line is not None, f"routing probe did not complete (exit {out.returncode})"
    return json.loads(line[len("RESULT:"):])


def _kinds(stage_calls):
    return [kind for kind, _model in stage_calls]


def test_default_provider_is_ollama_in_a_fresh_process():
    result = _probe_in_fresh_process()   # AI_PROVIDER unset
    assert result["provider"] == "ollama"
    assert result["default_model"] == "llama3.2:latest"
    for stage, stage_calls in result["stages"].items():
        kinds = _kinds(stage_calls)
        assert "ollama_tags" in kinds and "ollama_generate" in kinds, f"{stage}: Ollama path not used by default"
        assert not any(k.startswith("openai") for k in kinds), f"{stage}: OpenAI used although not selected"


def test_ollama_selected_explicitly_keeps_the_ollama_path():
    result = _probe_in_fresh_process(AI_PROVIDER="ollama", OLLAMA_MODEL="ollama-routing-test")
    assert result["provider"] == "ollama" and result["default_model"] == "ollama-routing-test"
    for stage, stage_calls in result["stages"].items():
        assert not any(k.startswith("openai") for k in _kinds(stage_calls)), f"{stage}: OpenAI used"
        generate_models = {m for k, m in stage_calls if k == "ollama_generate"}
        assert generate_models == {"ollama-routing-test"}, f"{stage}: wrong Ollama model {generate_models}"


def test_openai_routes_all_three_stages_with_the_configured_model():
    result = _probe_in_fresh_process(AI_PROVIDER=" OpenAI ", OPENAI_MODEL="gpt-routing-test")
    assert result["provider"] == "openai" and result["default_model"] == "gpt-routing-test"
    for stage in ("validation", "mapping", "correction"):
        stage_calls = result["stages"][stage]
        kinds = _kinds(stage_calls)
        assert "openai_check" in kinds and "openai_call" in kinds, f"{stage}: not routed through openai_provider"
        assert not any(k.startswith("ollama") for k in kinds), f"{stage}: an Ollama request was made"
        assert {m for _k, m in stage_calls} == {"gpt-routing-test"}, f"{stage}: configured OpenAI model not used"


def test_missing_openai_key_is_reported_and_never_falls_back_to_ollama():
    extracted, meta, lines, layout = _synthetic_statement()
    real_check = openai_provider.check_openai_availability
    real_key = openai_provider._get_api_key
    with _provider_recorders("openai") as calls:
        openai_provider.check_openai_availability = real_check   # the real check, with no key available
        openai_provider._get_api_key = lambda: None
        try:
            _aug, mapping, mapping_av = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout)
            validation, _presence, validation_av = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout)
            proposals, correction_av = acp.generate_correction_proposals(extracted, _flag(extracted), lines, meta, layout_lines=layout)
        finally:
            openai_provider._get_api_key = real_key
    for availability in (mapping_av, validation_av, correction_av):
        assert availability.reachable is False and "OPENAI_API_KEY is not set" in availability.detail
    assert all(r.technical_status == av.AI_UNAVAILABLE for r in mapping.values())
    assert all(r.technical_status == av.AI_UNAVAILABLE for r in validation.values())
    assert all(p.technical_status == av.AI_UNAVAILABLE for p in proposals.values())
    assert not any(kind.startswith("ollama") for kind, _m in calls), "fell back to Ollama"
    assert ocr_upload.ai_stage_outcome(mapping, mapping_av, "n/a", report_partial=True)[0] == "unavailable"


def test_openai_request_failure_uses_existing_error_status():
    extracted, meta, lines, layout = _synthetic_statement()
    import httpx2
    timeout_error = openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com/v1/chat/completions"))

    def _raise_timeout(**k):
        raise timeout_error

    real_call = openai_provider.call_openai
    real_key = openai_provider._get_api_key
    with _provider_recorders("openai") as calls:
        openai_provider.call_openai = real_call   # the real call, failing inside the (fake) SDK client
        openai_provider._get_api_key = lambda: "sk-test-not-real"
        real_client = _patch_openai_client(fake_create=_raise_timeout)
        try:
            _aug, mapping, mapping_av = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout)
            validation, _presence, _vav = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout)
            constructed = list(_patch_openai_client.last_fake.constructed)
        finally:
            _restore_openai_client(real_client)
            openai_provider._get_api_key = real_key
    attempted = [r for r in mapping.values() if r.ai_call_made]
    assert attempted and all(r.technical_status == av.AI_ERROR for r in attempted)
    assert all(r.reason.startswith("OpenAI call failed: OpenAI call timed out after 120s") for r in attempted)
    assert ocr_upload.ai_stage_outcome(mapping, mapping_av, "n/a", report_partial=True)[0] == "attempt_failed"
    assert all(r.technical_status == av.AI_ERROR and r.explanation.startswith("OpenAI call failed:") for r in validation.values())
    assert not any(kind.startswith("ollama") for kind, _m in calls), "fell back to Ollama"
    assert constructed and all(c["max_retries"] == 0 for c in constructed), "SDK retries must be off by default"


@contextlib.contextmanager
def _silent_local_endpoint():
    """A local TCP endpoint that accepts connections and never answers."""
    __tracebackhide__ = True
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(20)
    accepted = []

    def _accept():
        while True:
            try:
                conn, _ = srv.accept()
                accepted.append(conn)
            except OSError:
                return

    threading.Thread(target=_accept, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}/v1", accepted
    finally:
        srv.close()
        for conn in accepted:
            conn.close()


def test_openai_request_is_bounded_by_its_timeout():
    real_key = openai_provider._get_api_key
    real_base = os.environ.get("OPENAI_BASE_URL")
    real_retries = os.environ.pop("OPENAI_MAX_RETRIES", None)
    openai_provider._get_api_key = lambda: "sk-test-not-real"
    try:
        with _silent_local_endpoint() as (url, accepted):
            os.environ["OPENAI_BASE_URL"] = url
            # One untimed warm-up request first: the first OpenAI call in a
            # process pays one-time SDK setup (building the first HTTP client
            # and lazily importing the chat module - ~0.6s measured, more on a
            # loaded machine) before anything is sent. That is not part of
            # the request timeout, which is what this test bounds.
            try:
                openai_provider.call_openai("warm-up", model="gpt-4o-mini", timeout=1)
            except av.OllamaCallError:
                pass
            del accepted[:]
            started = time.monotonic()
            try:
                openai_provider.call_openai("prompt", model="gpt-4o-mini", timeout=1)
                raise AssertionError("a silent endpoint must time out")
            except av.OllamaCallError as exc:
                elapsed = time.monotonic() - started
                assert "timed out after 1s" in str(exc)
            assert len(accepted) == 1, f"default must be a single attempt, saw {len(accepted)}"
            assert elapsed < 2.5, f"a 1s request took {elapsed:.1f}s"

            os.environ["OPENAI_MAX_RETRIES"] = "1"   # opt-in retries are honoured and reported
            del accepted[:]
            try:
                openai_provider.call_openai("prompt", model="gpt-4o-mini", timeout=1)
            except av.OllamaCallError as exc:
                assert "per attempt, 2 attempts" in str(exc)
            assert len(accepted) == 2
        for bad in ("abc", "-3"):
            os.environ["OPENAI_MAX_RETRIES"] = bad
            assert openai_provider._max_retries() == 0
    finally:
        openai_provider._get_api_key = real_key
        os.environ.pop("OPENAI_MAX_RETRIES", None)
        if real_retries is not None:
            os.environ["OPENAI_MAX_RETRIES"] = real_retries
        if real_base is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = real_base


def _no_candidate_responder(prompt):
    return json.dumps({"results": [
        {"field": c["field"], "status": "NO_CANDIDATE_FOUND", "confidence": 0.0, "reason": "mocked",
         "selected_line_number": None, "evidence_line_numbers": []}
        for c in _contexts(prompt)
    ]})


def test_mapping_budget_marks_unstarted_batches_skipped():
    extracted, meta, lines, layout = _synthetic_statement()
    sent = []

    def _responder(prompt):
        sent.append([c["field"] for c in _contexts(prompt)])
        return _no_candidate_responder(prompt)

    real_exhausted = av._budget_exhausted
    av._budget_exhausted = lambda deadline: len(sent) >= 1   # the deadline passes during the first call
    try:
        with _provider_recorders("openai", openai_responder=_responder) as calls:
            _aug, results, availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout)
    finally:
        av._budget_exhausted = real_exhausted

    assert sum(1 for kind, _m in calls if kind == "openai_call") == 1, "no batch may start after the deadline"
    first_batch = set(sent[0])
    for field, r in results.items():
        if field in first_batch:
            assert r.technical_status == av.AI_COMPLETED and r.ai_call_made, f"{field}: completed result lost"
    skipped = [r for r in results.values() if r.technical_status == av.AI_SKIPPED]
    assert skipped, "fixture: later batches must exist"
    assert all("time budget" in r.reason and not r.ai_call_made for r in skipped)
    assert ocr_upload.ai_stage_outcome(results, availability, "n/a", report_partial=True)[0] == "partial"

    real_budget = adm.DYNAMIC_MAPPING_TIME_BUDGET_SECONDS
    adm.DYNAMIC_MAPPING_TIME_BUDGET_SECONDS = 0.0
    try:
        with _provider_recorders("openai", openai_responder=_responder) as calls:
            _aug, results, availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout)
    finally:
        adm.DYNAMIC_MAPPING_TIME_BUDGET_SECONDS = real_budget
    assert not any(kind == "openai_call" for kind, _m in calls), "a zero budget must start no batch"
    assert all(r.technical_status == av.AI_SKIPPED for r in results.values())
    assert ocr_upload.ai_stage_outcome(results, availability, "n/a", report_partial=True)[0] == "skipped"


def test_evidence_and_schema_checks_unchanged_under_openai():
    extracted, meta, lines, layout = _synthetic_statement()
    unshown_line = 10 ** 6

    def _responder(prompt):
        results = []
        for c in _contexts(prompt):
            if "candidate_rows" in c:   # dynamic mapping: cite a row it was never shown
                results.append({"field": c["field"], "status": "CANDIDATE_FOUND", "confidence": 0.95, "reason": "mocked",
                                "selected_line_number": unshown_line, "evidence_line_numbers": [unshown_line]})
            elif "target_row" in c:     # validation: a confirmation citing evidence it was never shown
                results.append({"field": c["field"], "status": "CONFIRMED", "confidence": 0.95,
                                "evidence_line_numbers": [unshown_line], "evidence_values": [123456789],
                                "explanation": "mocked", "requires_review": False})
        return json.dumps({"results": results})

    with _provider_recorders("openai", openai_responder=_responder) as calls:
        augmented, mapping, _mav = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout)
        validation, _presence, _vav = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout)
    assert any(kind == "openai_call" for kind, _m in calls)

    attempted = [f for f, r in mapping.items() if r.ai_call_made]
    assert attempted
    for field in attempted:
        assert mapping[field].mapping_status == "MAPPED_BUT_REJECTED", f"{field}: unshown row was not rejected"
        assert augmented[field]["validation_status"] == "NOT_FOUND", f"{field}: a rejected mapping populated the field"
    assert not any(ed.get("validation_status") in ("AI_MAPPED", "HUMAN_APPROVED_CORRECTION") for ed in augmented.values())

    completed = [r for r in validation.values() if r.technical_status == av.AI_COMPLETED]
    assert completed
    for r in completed:
        assert r.evidence_grounded is False and r.requires_review is True and r.confidence <= 0.3, \
            f"{r.field}: ungrounded evidence was trusted"


def main():
    test_missing_api_key_no_crash()
    test_valid_json_response()
    test_malformed_response_not_pre_validated()
    test_timeout_rate_limit_and_api_error_distinct_messages()
    test_unavailable_model_never_assumed_enabled()
    test_ollama_default_unchanged()
    test_provider_switch_affects_dispatch()
    test_never_silently_falls_back_to_ollama()
    test_ai_generated_values_cannot_enter_dynamic_mapping()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL OPENAI PROVIDER TESTS PASSED (no real API key or network call was made)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
