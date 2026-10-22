"""
Unit tests for field_glossary.py - the centralized Business Glossary
lookup shown on the Data Review page's Field-Level Review table.

No mocking framework, matching this codebase's own convention.

Run: python test_field_glossary.py
"""
import sys

import field_glossary as fg
import ocr_pipeline as op

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def test_exact_lookup_across_statements():
    print("=" * 100)
    print("TEST - exact lookup for a representative field from each statement section")
    print("=" * 100)
    cases = {
        "Revenue from operations": "PNL",
        "Total assets": "BS",
        "Operating cash flow": "CF",
        "DSCR": "RATIO",
    }
    for field, section in cases.items():
        definition = fg.get_definition(field)
        _ok(definition != fg._FALLBACK_DEFINITION, f"{field} ({section}) has a real configured definition")
        _ok(isinstance(definition, str) and len(definition) > 0, f"{field}'s definition is a non-empty string")


def test_every_template_field_covered():
    print()
    print("=" * 100)
    print("TEST - every real template field (ocr_pipeline ROW_MAPPING + EXTRA_FIELDS) has a definition")
    print("=" * 100)
    fields = list(op.ROW_MAPPING.keys()) + op.EXTRA_FIELDS
    missing = [f for f in fields if fg.get_definition(f) == fg._FALLBACK_DEFINITION]
    _ok(len(fields) > 0, "fixture sanity check: the template has fields to check")
    _ok(missing == [], f"no template field falls back to the neutral default, missing={missing}")


def test_fallback_for_unconfigured_field():
    print()
    print("=" * 100)
    print("TEST - an unconfigured/unknown field name gets the neutral fallback, never an invented definition")
    print("=" * 100)
    _ok(fg.get_definition("Totally Made Up Field Name") == "Definition not configured",
        "an unknown field name returns the exact neutral fallback string")
    _ok(fg.get_definition(None) == "Definition not configured", "None is handled safely, same fallback")
    _ok(fg.get_definition("") == "Definition not configured", "empty string is handled safely, same fallback")


def test_normalized_lookup_tolerates_whitespace_and_case():
    print()
    print("=" * 100)
    print("TEST - lookup tolerates surrounding whitespace and case differences (never a different field's definition)")
    print("=" * 100)
    canonical = fg.get_definition("Revenue from operations")
    _ok(fg.get_definition("  Revenue from operations  ") == canonical,
        "surrounding whitespace does not change the result")
    _ok(fg.get_definition("REVENUE FROM OPERATIONS") == canonical,
        "case differences do not change the result")
    _ok(fg.get_definition("revenue from operations") == canonical,
        "lowercase still resolves to the same real definition")


def test_definitions_never_mutated_by_lookup():
    print()
    print("=" * 100)
    print("TEST - lookups never mutate the underlying glossary dict (read-only reference data)")
    print("=" * 100)
    import copy
    snapshot = copy.deepcopy(fg.FIELD_DEFINITIONS)
    fg.get_definition("Total assets")
    fg.get_definition("unknown field")
    fg.get_definition(None)
    _ok(fg.FIELD_DEFINITIONS == snapshot, "FIELD_DEFINITIONS is unchanged after several lookups")


def main():
    test_exact_lookup_across_statements()
    test_every_template_field_covered()
    test_fallback_for_unconfigured_field()
    test_normalized_lookup_tolerates_whitespace_and_case()
    test_definitions_never_mutated_by_lookup()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL FIELD GLOSSARY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
