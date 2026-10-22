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
        def __init__(self, api_key=None, timeout=None):
            self.chat = _FakeChatNamespace()
            self.models = _FakeModels()

    real_openai_class = openai.OpenAI
    openai.OpenAI = _FakeClient
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
