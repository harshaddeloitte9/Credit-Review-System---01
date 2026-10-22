"""
Unit tests for ai_dynamic_mapping.py (AI-Assisted Dynamic Financial
Data Mapping phase).

IMPORTANT monkeypatch note: like ai_correction_proposals.py, this
module imports check_ollama_availability/call_ollama FROM ai_validation
via `from ai_validation import (...)` - a `from X import Y` copies the
reference into THIS module's own namespace at import time. Every
monkeypatch below is applied to `adm.check_ollama_availability` /
`adm.call_ollama` directly, never to the `ai_validation` module.

Uses the real, cached Tata Chemicals OCR text (same document used
throughout the AI-validation/correction-proposal phases) as the primary
fixture where possible - its 9 real NOT_FOUND fields (confirmed in
earlier phases to exist in the source under different wording) give a
realistic, already-characterized basis for these tests, gracefully
skipped if that cache isn't present on the machine (matching
test_extraction_audit.py's own convention). Small synthetic fixtures
cover the two scenarios that need a controlled edge case (ambiguous
candidates, the candidate-row cap).

No mocking framework, matching this codebase's own convention. Never
calls a real Ollama instance.

Run: python test_ai_dynamic_mapping.py
"""

import copy
import glob
import json
import os
import re
import sys
import tempfile

import ai_dynamic_mapping as adm
import ai_validation as av
import ocr_pipeline as op

_FAILURES = []
_FIELD_RE = re.compile(r'"field":\s*"([^"]*)"')


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def _parse_contexts(prompt):
    return json.loads(prompt.split("FIELDS:\n", 1)[1])


def _find_tata_chemicals_cache():
    candidates = glob.glob(os.path.join(tempfile.gettempdir(), "credit_review_tool_pdf_upload", "raw_ocr_cache_*.txt"))
    for path in candidates:
        if os.path.getsize(path) <= 50_000:
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if "tata chemicals" in text.lower():
            return text
    return None


def _build_lines_for_ai(text):
    raw_and_normalized = [(raw, op.normalize_text(raw)) for raw in text.splitlines()]
    raw_and_normalized = [(raw, norm) for raw, norm in raw_and_normalized if norm]
    lines = [norm for _raw, norm in raw_and_normalized]
    layout_lines = [op._normalize_preserve_layout(raw) for raw, _norm in raw_and_normalized]
    return lines, layout_lines


_TATA_CHEMICALS_TEXT = _find_tata_chemicals_cache()


def _real_fixture():
    extracted, _debug, meta = op.extract_financial_values(_TATA_CHEMICALS_TEXT, op.TEMPLATE_FILE)
    lines, layout_lines = _build_lines_for_ai(_TATA_CHEMICALS_TEXT)
    return extracted, meta, lines, layout_lines


def _make_fake_call(keyword_by_field, default_status="NO_CANDIDATE_FOUND", confidence=0.8, extra_fields=None):
    """Builds a fake call_ollama that, for each field in a batch, looks
    for a candidate row containing `keyword_by_field[field]` and selects
    it by line number ONLY - never a value, matching the real (redesigned)
    contract. `extra_fields`, if given, is merged into every
    CANDIDATE_FOUND result dict - used to prove that even if a raw
    response smuggles in an old-style value/column key, it can never
    reach a populated field (see test_8)."""
    def _fake(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            field = ctx["field"]
            keyword = keyword_by_field.get(field)
            candidates = ctx.get("candidate_rows") or []
            match = next((c for c in candidates if keyword and keyword.lower() in c["text"].lower()), None)
            if match is None:
                results.append({
                    "field": field, "status": default_status, "confidence": 0.5,
                    "reason": "no plausible candidate found", "selected_line_number": None,
                    "evidence_line_numbers": [],
                })
                continue
            result = {
                "field": field, "status": "CANDIDATE_FOUND", "confidence": confidence,
                "reason": f"'{match['text'][:40]}' looks semantically equivalent to {field}.",
                "selected_line_number": match["line_number"],
                "evidence_line_numbers": [match["line_number"]],
            }
            if extra_fields:
                result.update(extra_fields)
            results.append(result)
        return json.dumps({"results": results})
    return _fake


def _patch(fake_check, fake_call):
    real = {"check": adm.check_ollama_availability, "call": adm.call_ollama}
    adm.check_ollama_availability = fake_check
    adm.call_ollama = fake_call
    return real


def _restore(real):
    adm.check_ollama_availability = real["check"]
    adm.call_ollama = real["call"]


def _available(**k):
    return av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")


def test_0_rank_candidates_unit():
    print("=" * 100)
    print("TEST 0 - _rank_candidates() is a pure, deterministic scoring function")
    print("=" * 100)
    # "Capex"'s real ocr_pipeline.ALIASES are "purchase of capital assets"/
    # "purchase of capital asset"/"capital expenditure" - reusing the
    # SAME vocabulary the module already sends to Ollama, no invented data.
    candidates = [
        {"line_number": 10, "text": "Capital expenditure incurred during the year", "raw_values": [100, 90]},
        {"line_number": 11, "text": "Employee benefit expenses", "raw_values": [20, 18]},
        {"line_number": 12, "text": "Depreciation and amortisation", "raw_values": [5, 4]},
    ]
    ranked = adm._rank_candidates("Capex", candidates)
    _ok(ranked[0]["line_number"] == 10, "the row lexically closest to a real Capex alias scores highest")
    _ok(ranked[0]["score"] > ranked[1]["score"] >= ranked[2]["score"], "scores are ordered descending")
    _ok("capital" in ranked[0]["reason"].lower() or "similarity" in ranked[0]["reason"].lower(),
        "the winning candidate's reason names what matched")
    _ok(adm._rank_candidates("Capex", candidates) == ranked, "ranking is deterministic (same input -> same output)")


def test_1_2_exact_and_alias_match_untouched():
    print("=" * 100)
    print("TEST 1/2 - exact/alias match fields are never touched by dynamic mapping")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()
    snapshot = copy.deepcopy(extracted)

    def _unreachable(**k):
        return av.OllamaAvailability(False, False, [], "unreachable")

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked when Ollama is unreachable")

    real = _patch(_unreachable, _should_not_be_called)
    try:
        augmented, mapping_results, availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        _restore(real)

    _ok("Revenue from operations" not in mapping_results, "an exact-alias-matched field (Revenue) never gets a mapping attempt")
    _ok(augmented["Revenue from operations"] == snapshot["Revenue from operations"], "its extracted record is byte-for-byte unchanged")
    _ok(extracted == snapshot, "the original extracted dict passed in is never mutated")


def test_2_correct_row_selection_deterministic_value_extraction():
    print()
    print("=" * 100)
    print("TEST 2 - AI selects the correct row; Python deterministically extracts its values")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()
    snapshot = copy.deepcopy(extracted)

    fake_call = _make_fake_call({"Capex": "Acquisition of property"})
    real = _patch(_available, fake_call)
    try:
        augmented, mapping_results, availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        _restore(real)

    r = mapping_results["Capex"]
    _ok(r.mapping_status == "MAPPED_AND_VERIFIED", f"Capex is mapped and verified via semantic row selection, got {r.mapping_status!r}")
    _ok(r.evidence_grounded is not False, "evidence is grounded (or not required)")
    _ok(augmented["Capex"]["validation_status"] == "AI_MAPPED", "augmented dict shows the new AI_MAPPED status")

    # Cross-check against the SAME deterministic parser, computed
    # independently right here in the test (not hand-copied), to prove
    # the populated value is exactly what Python read off the real row -
    # never anything Ollama could have supplied (it was never asked).
    structure = av._resolve_document_structure(lines, layout_lines, meta)
    section, header_info, current_x, previous_x, _cs = av._section_for_field("Capex", structure)
    expected_count = header_info["count"] if header_info else None
    tokens = op.extract_number_tokens(lines[r.matched_source_line_number])
    tokens = op._with_layout_offsets(tokens, layout_lines[r.matched_source_line_number])
    expected_current, expected_previous, _conf, _filtered, _status = op.select_fy_values(
        tokens, expected_count, current_x, previous_x
    )
    _ok(augmented["Capex"]["fy_current"] == expected_current and augmented["Capex"]["fy_previous"] == expected_previous,
        f"populated values ({augmented['Capex']['fy_current']}, {augmented['Capex']['fy_previous']}) exactly match "
        f"the deterministic parser's own independent computation ({expected_current}, {expected_previous})")
    _ok(extracted == snapshot, "the original extracted dict is never mutated")
    _ok(augmented["Revenue from operations"] == snapshot["Revenue from operations"], "unrelated fields are untouched")


def test_4_ambiguous_candidates():
    print()
    print("=" * 100)
    print("TEST 4 - ambiguous candidates: model declines to guess -> INSUFFICIENT_CONTEXT")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()

    def _fake_undecided(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            results.append({
                "field": ctx["field"], "status": "INSUFFICIENT_CONTEXT", "confidence": 0.4,
                "reason": "Two candidate rows look equally plausible - cannot confidently choose.",
                "selected_line_number": None, "evidence_line_numbers": [],
            })
        return json.dumps({"results": results})

    real = _patch(_available, _fake_undecided)
    try:
        _augmented, mapping_results, _availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        _restore(real)

    r = mapping_results["Capex"]
    _ok(r.mapping_status == "INSUFFICIENT_CONTEXT", f"model declining to choose -> INSUFFICIENT_CONTEXT, got {r.mapping_status!r}")


def test_5_wrong_year_mapping_corrected_deterministically():
    print()
    print("=" * 100)
    print("TEST 5 - year assignment no longer depends on anything Ollama says: a row printing")
    print("its two years in REVERSED order is still assigned correctly by the deterministic")
    print("column-position parser (ocr_pipeline.select_fy_values), not by an AI-stated label")
    print("(there is no such label in the contract any more).")
    print("=" * 100)

    # A row whose real current-year value (547) is printed SECOND
    # (rightmost) and whose real previous-year value (1,057) is printed
    # FIRST (leftmost) - the reverse of this codebase's usual left=current
    # convention. Column offsets are computed from the same
    # layout-preserving string select_fy_values() itself compares
    # against, not hand-counted.
    raw_line = "    Capital expenditure incurred" + " " * 30 + "1,057" + " " * 15 + "547"
    lines_line = op.normalize_text(raw_line)
    layout_line = op._normalize_preserve_layout(raw_line)
    previous_offset = layout_line.find("1,057")
    current_offset = layout_line.find("547")
    _ok(previous_offset != -1 and current_offset != -1, "fixture sanity check: both figures are found in the layout line")

    fake_structure = {
        "section_by_type": {},
        "header_by_type": {"CF": None},
        "columns_by_type": {"CF": (current_offset, previous_offset, "data_median")},
        "bs_subsections": {},
    }
    meta = {"target_current_label": "FY 31.03.2026", "target_previous_label": "FY 31.03.2025"}
    v = {
        "status": "CANDIDATE_FOUND", "confidence": 0.8, "reason": "semantic match",
        "selected_line_number": 0, "evidence_line_numbers": [],
    }

    result = adm._finalize_mapping("Capex", v, {0}, [lines_line], [layout_line], meta, fake_structure)

    _ok(result.mapping_status == "MAPPED_AND_VERIFIED", f"reversed-order row is still confidently resolved, got {result.mapping_status!r}")
    _ok(result.matched_value_current == 547.0, f"fy_current is correctly the SECOND-printed figure (547), got {result.matched_value_current!r}")
    _ok(result.matched_value_previous == 1057.0, f"fy_previous is correctly the FIRST-printed figure (1,057), got {result.matched_value_previous!r}")


def test_6_unit_mismatch_flagged_not_converted():
    print()
    print("=" * 100)
    print("TEST 6 - unit mismatch (Lakhs) -> flagged, never silently rescaled")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()

    # Inject a synthetic "in Lakhs" marker into the real Capex row's own
    # text (monkeypatch layout_lines at that one index) so the unit
    # check has something real to detect, without touching any other
    # line or field.
    real_check_avail = adm.check_ollama_availability
    structure = adm.ai_validation._resolve_document_structure(lines, layout_lines, meta)
    section, _h, _cx, _px, _cs = adm.ai_validation._section_for_field("Capex", structure)
    target_idx = None
    for idx in range(section["start"], section["end"]):
        if "acquisition of property" in layout_lines[idx].lower():
            target_idx = idx
            break
    _ok(target_idx is not None, "fixture sanity check: found the real Capex row to inject a unit marker into")

    mutated_layout_lines = list(layout_lines)
    mutated_layout_lines[target_idx] = mutated_layout_lines[target_idx] + " (figures in Lakhs)"

    fake_call = _make_fake_call({"Capex": "Acquisition of property"})
    real = _patch(_available, fake_call)
    try:
        augmented, mapping_results, _availability = adm.map_missing_fields(
            extracted, lines, meta, layout_lines=mutated_layout_lines
        )
    finally:
        _restore(real)

    r = mapping_results["Capex"]
    _ok(r.mapping_status == "INSUFFICIENT_CONTEXT", f"unit mismatch -> INSUFFICIENT_CONTEXT (never auto-converted), got {r.mapping_status!r}")
    _ok("lakh" in r.reason.lower(), "the specific unit-mismatch reason is recorded")
    _ok(augmented["Capex"]["validation_status"] == "NOT_FOUND", "Capex stays NOT_FOUND rather than being silently rescaled")


def test_3_wrong_source_row_rejected():
    print()
    print("=" * 100)
    print("TEST 3 - AI hallucinated/wrong row (line never shown) -> rejected, no crash")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()

    def _fake_hallucinate_line(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            results.append({
                "field": ctx["field"], "status": "CANDIDATE_FOUND", "confidence": 0.8,
                "reason": "x", "selected_line_number": 999999,
                "evidence_line_numbers": [999999],
            })
        return json.dumps({"results": results})

    real = _patch(_available, _fake_hallucinate_line)
    try:
        augmented, mapping_results, _availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
        crashed = False
    except Exception as exc:
        crashed = True
        mapping_results, augmented = {}, {}
        print(f"    unexpected exception: {exc}")
    finally:
        _restore(real)

    _ok(not crashed, "a hallucinated out-of-range line number never crashes the application")
    _ok(mapping_results.get("Capex") is not None and mapping_results["Capex"].mapping_status == "MAPPED_BUT_REJECTED",
        f"hallucinated line -> rejected, got {mapping_results.get('Capex') and mapping_results['Capex'].mapping_status!r}")


def test_8_ollama_cannot_return_a_value():
    print()
    print("=" * 100)
    print("TEST 8 - a hallucinated value is STRUCTURALLY impossible to populate: even when a raw")
    print("response smuggles in an old-style value key alongside a correct row selection, it is")
    print("never read - the populated value is always Python's own deterministic parse.")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()

    # A malicious/legacy-format response: a correct selected_line_number,
    # but ALSO an extra "matched_value_current": 999999 key - a value
    # that never appears anywhere in the real document. The new schema
    # (_REQUIRED_MAPPING_KEYS/_validate_mapping_response) has no slot for
    # this key at all, so it is silently dropped before it can influence
    # anything.
    fake_call = _make_fake_call(
        {"Capex": "Acquisition of property"},
        extra_fields={"matched_value_current": 999999, "matched_value_previous": 999999},
    )
    real = _patch(_available, fake_call)
    try:
        augmented, mapping_results, _availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        _restore(real)

    r = mapping_results["Capex"]
    _ok(r.mapping_status == "MAPPED_AND_VERIFIED", f"correct row selection still verifies fine despite the smuggled key, got {r.mapping_status!r}")
    _ok(augmented["Capex"]["fy_current"] != 999999 and augmented["Capex"]["fy_previous"] != 999999,
        "the smuggled fake value (999999) never reaches the populated field")
    _ok(r.matched_value_current not in (999999, None) and r.matched_value_previous not in (999999, None),
        f"the result's own values are Python's real deterministic parse ({r.matched_value_current}, {r.matched_value_previous}), not the smuggled key")


def test_9_ai_unavailable_stays_not_found():
    print()
    print("=" * 100)
    print("TEST 9 - Ollama unavailable: every NOT_FOUND field stays exactly NOT_FOUND")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()
    snapshot = copy.deepcopy(extracted)

    def _unreachable(**k):
        return av.OllamaAvailability(False, False, [], "Ollama is not reachable. Is 'ollama serve' running?")

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked when Ollama is unreachable")

    real = _patch(_unreachable, _should_not_be_called)
    try:
        augmented, mapping_results, availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        _restore(real)

    _ok(augmented == snapshot, "the augmented dict is identical to the original - nothing was populated")
    _ok(all(r.technical_status == "AI_UNAVAILABLE" for r in mapping_results.values()), "every NOT_FOUND field reports AI_UNAVAILABLE")
    _ok(availability is not None and not availability.reachable, "availability correctly reports unreachable")


def test_10_candidate_cap_scalability():
    print()
    print("=" * 100)
    print("TEST 10 - 500-page scalability: an oversized candidate list is capped, not sent")
    print("=" * 100)

    real_cap = adm.MAX_CANDIDATE_ROWS
    adm.MAX_CANDIDATE_ROWS = 3

    lines = [
        "Statement of Standalone Audited Balance Sheet as at 31 March, 2026",
        "Particulars" + " " * 49 + "As at".rjust(18) + "As at".rjust(18),
        " " * 60 + "31".rjust(18) + "31".rjust(18),
        " " * 60 + "March,".rjust(18) + "March,".rjust(18),
        " " * 60 + "2026".rjust(18) + "2025".rjust(18),
        "I. ASSETS",
    ] + [f"    Line item {i}" + " " * 40 + f"{i}".rjust(18) + f"{i+1}".rjust(18) for i in range(10)]
    text = "\n".join(lines)
    extracted, _debug, meta = op.extract_financial_values(text, op.TEMPLATE_FILE)
    ai_lines, ai_layout_lines = _build_lines_for_ai(text)

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked once the candidate cap is exceeded")

    real = _patch(_available, _should_not_be_called)
    try:
        _augmented, mapping_results, _availability = adm.map_missing_fields(extracted, ai_lines, meta, layout_lines=ai_layout_lines)
    finally:
        _restore(real)
        adm.MAX_CANDIDATE_ROWS = real_cap

    _ok("Cash" in mapping_results, "Cash (genuinely NOT_FOUND in this synthetic doc) got a mapping attempt")
    _ok(mapping_results["Cash"].mapping_status == "INSUFFICIENT_CONTEXT",
        f"an oversized candidate section (10 rows > cap of 3) -> INSUFFICIENT_CONTEXT, not a huge prompt, got {mapping_results['Cash'].mapping_status!r}")


def test_11_rejecting_ai_mapped_field_reverts_it():
    print()
    print("=" * 100)
    print("TEST 11 - rejecting an AI-mapped field truly reverts it to NOT_FOUND")
    print("=" * 100)
    import reviewed_dataset

    extracted = {
        "Capex": {
            "fy_current": 547.0, "fy_previous": 1057.0, "normalized": False, "ambiguous": False,
            "validation_status": "AI_MAPPED", "extraction_method": "ai_mapped",
            "source_line_number": 1444, "source_text": "Acquisition of property... 547 1,057",
            "confidence": "medium",
        },
    }
    approved_corrections = {
        "Capex": {
            "decision": "rejected", "proposed_value_current": 547.0, "proposed_value_previous": 1057.0,
            "proposed_source_line_number": 1444, "proposed_source_text": "Acquisition of property... 547 1,057",
            "original_value_current": None, "original_value_previous": None, "original_source_line_number": None,
            "reason": "x", "timestamp": "2026-01-01T00:00:00",
        },
    }

    _ok(reviewed_dataset.needs_reviewed_workbook(extracted, approved_corrections),
        "a rejected decision on an AI_MAPPED field DOES require regenerating the reviewed workbook")

    reviewed = reviewed_dataset.build_reviewed_extracted(extracted, approved_corrections)
    _ok(reviewed["Capex"]["validation_status"] == "NOT_FOUND", "the rejected AI-mapped field is reverted to NOT_FOUND")
    _ok(reviewed["Capex"]["fy_current"] is None and reviewed["Capex"]["fy_previous"] is None,
        "the rejected AI-mapped field's values are cleared, not left as the AI's guess")
    _ok(extracted["Capex"]["validation_status"] == "AI_MAPPED", "the original extracted dict is never mutated")

    # Sanity check the OTHER branch still works: accepting an AI_MAPPED
    # field applies its value and marks it HUMAN_APPROVED_CORRECTION.
    accepted = {"Capex": dict(approved_corrections["Capex"], decision="accepted")}
    reviewed_accepted = reviewed_dataset.build_reviewed_extracted(extracted, accepted)
    _ok(reviewed_accepted["Capex"]["validation_status"] == "HUMAN_APPROVED_CORRECTION", "accepting an AI-mapped field marks it HUMAN_APPROVED_CORRECTION")
    _ok(reviewed_accepted["Capex"]["fy_current"] == 547.0, "accepting an AI-mapped field keeps its verified value")


def test_12_correct_candidate_ranks_in_topk():
    print()
    print("=" * 100)
    print("TEST 12 - the real Capex row ranks inside the Top-K shortlist actually sent to Ollama")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()
    structure = av._resolve_document_structure(lines, layout_lines, meta)
    claimed = adm._claimed_line_numbers(extracted)
    section, _h, _cx, _px, _cs = av._section_for_field("Capex", structure)
    candidates = adm._discover_candidates(section, layout_lines, claimed)
    _ok(len(candidates) > adm.TOP_K_CANDIDATES, f"the real CF section has more candidates ({len(candidates)}) than TOP_K ({adm.TOP_K_CANDIDATES}) - a real narrowing case")

    ranked = adm._rank_candidates("Capex", candidates)
    true_line = next(c["line_number"] for c in candidates if "acquisition of property" in c["text"].lower())
    shortlisted_lines = {c["line_number"] for c in ranked[:adm.TOP_K_CANDIDATES]}
    _ok(true_line in shortlisted_lines, f"the real Capex row (line {true_line}) ranks inside the top {adm.TOP_K_CANDIDATES} of {len(candidates)} real candidates")


def test_13_correct_candidate_outside_topk_transparent():
    print()
    print("=" * 100)
    print("TEST 13 - a true match ranked OUTSIDE Top-K is excluded from the prompt but stays visible")
    print("=" * 100)
    real_k = adm.TOP_K_CANDIDATES
    adm.TOP_K_CANDIDATES = 3
    try:
        # A true match worded with ZERO lexical/token overlap against
        # Capex's own name/aliases, surrounded by decoys that DO share
        # tokens with "capital"/"purchase" and so out-score it.
        lines_text = [
            "Statement of Cash Flows",
            "Particulars" + " " * 40 + "31 March, 2026".rjust(20) + "31 March, 2025".rjust(20),
            "    Purchase of capital investments" + " " * 10 + "500".rjust(20) + "480".rjust(20),
            "    Purchase of capital instruments" + " " * 10 + "400".rjust(20) + "380".rjust(20),
            "    Purchase of capital securities" + " " * 11 + "300".rjust(20) + "280".rjust(20),
            "    Outflow towards new plant commissioning" + " " * 2 + "250".rjust(20) + "240".rjust(20),
        ]
        text = "\n".join(lines_text)
        _extracted2, _debug, meta = op.extract_financial_values(text, op.TEMPLATE_FILE)
        ai_lines, ai_layout_lines = _build_lines_for_ai(text)
        structure = av._resolve_document_structure(ai_lines, ai_layout_lines, meta)
        claimed = set()
        section, _h, _cx, _px, _cs = av._section_for_field("Capex", structure)
        _ok(section is not None, "fixture sanity check: Capex's CF section was located in this synthetic document")
        candidates = adm._discover_candidates(section, ai_layout_lines, claimed)
        true_line = next((c["line_number"] for c in candidates if "new plant commissioning" in c["text"].lower()), None)
        _ok(true_line is not None, "fixture sanity check: the zero-overlap true row exists among discovered candidates")

        ctx, context_line_numbers, _text, reason_code, ranking_info = adm._field_mapping_context(
            "Capex", structure, ai_layout_lines, claimed
        )
        _ok(ctx is not None, "enough real candidates exist to build a prompt context")
        _ok(true_line not in context_line_numbers, "the true row (lowest lexical/token overlap) is NOT sent to Ollama once Top-K narrows the list")
        ranked_lines = [r["line_number"] for r in ranking_info["candidate_rankings"]]
        _ok(true_line in ranked_lines, "the true row still appears in the full candidate_rankings for transparency")
        true_rank_entry = next(r for r in ranking_info["candidate_rankings"] if r["line_number"] == true_line)
        _ok("score" in true_rank_entry and "reason" in true_rank_entry, "the excluded true row still carries a score/reason a reviewer can inspect")
    finally:
        adm.TOP_K_CANDIDATES = real_k


def test_14_shortlist_reduces_what_is_sent_to_ollama():
    print()
    print("=" * 100)
    print("TEST 14 - only the Top-K shortlist (not all discovered candidates) is placed in the prompt")
    print("=" * 100)
    if _TATA_CHEMICALS_TEXT is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return
    extracted, meta, lines, layout_lines = _real_fixture()

    seen_candidate_counts = {}

    def _fake_capturing(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            field = ctx["field"]
            seen_candidate_counts[field] = len(ctx.get("candidate_rows") or [])
            results.append({
                "field": field, "status": "NO_CANDIDATE_FOUND", "confidence": 0.5, "reason": "x",
                "selected_line_number": None, "evidence_line_numbers": [],
            })
        return json.dumps({"results": results})

    real = _patch(_available, _fake_capturing)
    try:
        _augmented, mapping_results, _availability = adm.map_missing_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        _restore(real)

    r = mapping_results["Capex"]
    _ok(r.candidate_count > adm.TOP_K_CANDIDATES, f"Capex's real section discovered {r.candidate_count} candidates, more than TOP_K ({adm.TOP_K_CANDIDATES})")
    _ok(r.shortlisted_count == adm.TOP_K_CANDIDATES, f"exactly TOP_K ({adm.TOP_K_CANDIDATES}) were shortlisted, got {r.shortlisted_count}")
    _ok(seen_candidate_counts.get("Capex") == adm.TOP_K_CANDIDATES,
        f"Ollama's own prompt actually contained only {seen_candidate_counts.get('Capex')} candidate rows, not all {r.candidate_count}")
    _ok(len(r.candidate_rankings) == r.candidate_count, "the full ranking (all discovered candidates) is preserved on the result for transparency")


def test_15_row_selected_values_unresolved():
    print()
    print("=" * 100)
    print("TEST 15 - AI selects a genuinely correct, verified row, but this row's own number")
    print("cannot be confidently attributed to a fiscal year -> AI_ROW_SELECTED_VALUES_UNRESOLVED,")
    print("field stays NOT_FOUND, nothing is guessed.")
    print("=" * 100)

    # A single number placed EXACTLY at the midpoint between the two
    # calibrated columns - _closer_column() returns None for a value
    # that isn't clearly closer to either side, so select_fy_values()
    # cannot confidently assign it to either fiscal year.
    raw_line = "    Some ratio-like item" + " " * 20 + "42.5"
    lines_line = op.normalize_text(raw_line)
    layout_line = op._normalize_preserve_layout(raw_line)
    token_offset = layout_line.find("42.5")
    _ok(token_offset != -1, "fixture sanity check: the figure is found in the layout line")

    fake_structure = {
        "section_by_type": {},
        "header_by_type": {"CF": None},
        "columns_by_type": {"CF": (token_offset - 20, token_offset + 20, "data_median")},
        "bs_subsections": {},
    }
    meta = {"target_current_label": "FY 31.03.2026", "target_previous_label": "FY 31.03.2025"}
    v = {
        "status": "CANDIDATE_FOUND", "confidence": 0.7, "reason": "semantic match",
        "selected_line_number": 0, "evidence_line_numbers": [],
    }

    result = adm._finalize_mapping("Capex", v, {0}, [lines_line], [layout_line], meta, fake_structure)

    _ok(result.mapping_status == "AI_ROW_SELECTED_VALUES_UNRESOLVED",
        f"a correctly-selected but unparseable row gets its own distinct status, got {result.mapping_status!r}")
    _ok(result.matched_value_current is None and result.matched_value_previous is None,
        "nothing is guessed - no value is populated")
    _ok(result.matched_source_line_number == 0, "the row itself is still recorded for a human to inspect")


def main():
    test_0_rank_candidates_unit()
    test_1_2_exact_and_alias_match_untouched()
    test_2_correct_row_selection_deterministic_value_extraction()
    test_3_wrong_source_row_rejected()
    test_4_ambiguous_candidates()
    test_5_wrong_year_mapping_corrected_deterministically()
    test_6_unit_mismatch_flagged_not_converted()
    test_8_ollama_cannot_return_a_value()
    test_9_ai_unavailable_stays_not_found()
    test_10_candidate_cap_scalability()
    test_11_rejecting_ai_mapped_field_reverts_it()
    test_12_correct_candidate_ranks_in_topk()
    test_13_correct_candidate_outside_topk_transparent()
    test_14_shortlist_reduces_what_is_sent_to_ollama()
    test_15_row_selected_values_unresolved()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL AI DYNAMIC MAPPING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
