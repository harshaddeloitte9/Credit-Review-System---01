"""
OpenAI provider for the AI-assisted layers (ai_validation.py /
ai_dynamic_mapping.py / ai_correction_proposals.py) - an alternative to
the default Ollama provider, selected by setting AI_PROVIDER=openai
before the app starts. See OPENAI_TESTING.md for exact commands.

Architectural boundary: this module is PURELY a transport. It sends a
prompt string to OpenAI's chat-completions API and returns whatever raw
text comes back (or raises OllamaCallError) - it never parses,
validates, or trusts its own output. Every existing schema/evidence
check in the three AI-layer modules runs COMPLETELY UNCHANGED on
whatever this returns, exactly as it already does for Ollama's own
response text. This module has no code path that could generate or
populate a financial value itself - it is not asked to, and even if a
response somehow contained one, the same deterministic verification
every other provider's output goes through still applies downstream.

OllamaAvailability/OllamaCallError are reused as-is from ai_validation.py
(not duplicated or renamed) - both are already provider-neutral in
shape, so every existing `except OllamaCallError` block and every
existing OllamaAvailability-constructing test fixture keeps working
unchanged regardless of which provider actually ran.

The `openai` package itself, and this module's own use of it, are both
imported LAZILY (only inside the two functions below) - a default
Ollama-only run of this application never needs the package installed,
never needs an API key, and never imports this module at all (see the
two guard clauses in ai_validation.py's check_ollama_availability() /
call_ollama()).
"""
import os

from ai_validation import OllamaAvailability, OllamaCallError


def _get_api_key():
    """Checks the environment variable first, then Streamlit's own
    secrets mechanism (.streamlit/secrets.toml, already gitignored in
    this project) - never hardcoded, never logged, never returned in
    any error message. Safe to call outside a Streamlit context (e.g.
    from a plain test script) - st.secrets is only touched inside a
    try/except so the absence of a Streamlit runtime never crashes this
    lookup."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets.get("OPENAI_API_KEY")
    except Exception:
        return None


def _max_retries():
    """Retries per OpenAI request (OPENAI_MAX_RETRIES, default 0). The SDK
    otherwise retries timeouts, 408/409/429 and 5xx twice on its own, so
    one request could take about 3x the timeout the caller passed - and
    every AI stage only checks its time budget between batches. With 0,
    each request is bounded by its own timeout, like the Ollama path."""
    try:
        return max(0, int(os.environ.get("OPENAI_MAX_RETRIES", "0")))
    except ValueError:
        return 0


def check_openai_availability(model, timeout):
    """Mirrors ai_validation.check_ollama_availability()'s own contract
    and philosophy exactly: never raises, never assumes the configured
    model is actually enabled for this account - always verifies via a
    real (but cheap) API call before saying so."""
    api_key = _get_api_key()
    if not api_key:
        return OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail="OPENAI_API_KEY is not set (checked the environment variable and "
                   ".streamlit/secrets.toml). See OPENAI_TESTING.md.",
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        return OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail=f"The 'openai' package is not installed ({exc}). Run: pip install -r requirements.txt",
        )

    try:
        client = OpenAI(api_key=api_key, timeout=timeout, max_retries=_max_retries())
        page = client.models.list()
        installed = [m.id for m in page.data]
    except Exception as exc:
        return OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail=f"OpenAI API is not reachable ({exc.__class__.__name__}): {exc}",
        )

    if model not in installed:
        return OllamaAvailability(
            reachable=True, model_installed=False, installed_models=installed,
            detail=f"OpenAI is reachable, but model '{model}' is not available to this account/API key.",
        )

    return OllamaAvailability(reachable=True, model_installed=True, installed_models=installed, detail="")


def call_openai(prompt, model, timeout):
    """Mirrors ai_validation.call_ollama()'s own contract exactly:
    returns the raw response text (still untrusted - the caller's own
    _validate_*_response() does the real checking), raises
    OllamaCallError on any failure. Uses OpenAI's native structured-
    output JSON mode - every existing prompt in this project already
    instructs "return only JSON", which is this mode's own requirement,
    so no prompt text needs to change for either provider."""
    try:
        import openai
        from openai import OpenAI
    except ImportError as exc:
        raise OllamaCallError(f"The 'openai' package is not installed: {exc}") from exc

    api_key = _get_api_key()
    if not api_key:
        raise OllamaCallError("OPENAI_API_KEY is not set.")

    retries = _max_retries()
    client = OpenAI(api_key=api_key, timeout=timeout, max_retries=retries)

    # Caught most-specific-first (AuthenticationError/RateLimitError/
    # NotFoundError are subclasses of APIStatusError/APIError;
    # APITimeoutError is a subclass of APIConnectionError) so each
    # failure gets its own clear, distinguishable message rather than a
    # single generic "API error" - verified against the installed SDK's
    # actual exception hierarchy during implementation.
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
    except openai.AuthenticationError as exc:
        raise OllamaCallError(f"OpenAI authentication failed - check OPENAI_API_KEY ({exc.__class__.__name__}): {exc}") from exc
    except openai.RateLimitError as exc:
        raise OllamaCallError(f"OpenAI rate limit exceeded ({exc.__class__.__name__}): {exc}") from exc
    except openai.NotFoundError as exc:
        raise OllamaCallError(f"OpenAI model '{model}' not found / not available to this account ({exc.__class__.__name__}): {exc}") from exc
    except openai.APITimeoutError as exc:
        attempts = f" per attempt, {retries + 1} attempts" if retries else ""
        raise OllamaCallError(f"OpenAI call timed out after {timeout}s{attempts} ({exc.__class__.__name__}): {exc}") from exc
    except openai.APIConnectionError as exc:
        raise OllamaCallError(f"Could not connect to the OpenAI API ({exc.__class__.__name__}): {exc}") from exc
    except openai.APIError as exc:
        raise OllamaCallError(f"OpenAI API error ({exc.__class__.__name__}): {exc}") from exc
    except Exception as exc:
        raise OllamaCallError(f"OpenAI call failed ({exc.__class__.__name__}): {exc}") from exc

    return resp.choices[0].message.content or ""
