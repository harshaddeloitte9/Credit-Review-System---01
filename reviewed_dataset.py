"""
Canonical REVIEWED dataset (final integration pass).

Builds a single, explicit "reviewed_extracted" layer from the raw
deterministic `extracted` dict plus whatever mapping-correction
proposals a human has explicitly accepted on the Data Review page, and
regenerates the same kind of merged workbook ocr_upload.py itself
produces - so every downstream module (financial_analysis.py,
risk_scoring.py, covenants.py, collateral_assessment.py,
continuous_monitoring.py, portfolio_monitoring.py, credit_memo.py)
reads ONE consistent dataset through the existing reference_data.py
bundle-loading plumbing, with ZERO changes to any of those modules.

Architectural guarantees:
  - The original `extracted` dict is never mutated - build_reviewed_
    extracted() always returns a fresh dict; the original PDF-mode
    merged workbook ocr_upload.py produced is never touched or
    overwritten either.
  - For a field that started out deterministically OK/REVIEW_REQUIRED,
    only an "accepted" decision changes it - "rejected"/"kept_original"
    are preserved (for the audit trail the Data Review page shows) but
    never change what flows downstream, since the pre-existing
    deterministic value already sitting in `extracted[field]` is
    exactly what should keep flowing.
  - For a field ai_dynamic_mapping.py populated (validation_status ==
    "AI_MAPPED"), `extracted[field]` already contains the AI's guess by
    the time a human reviews it - unlike the case above, a "rejected"/
    "kept_original" decision here must actively REVERT the field to a
    genuine NOT_FOUND state, or the AI's rejected guess would silently
    keep flowing downstream just because nothing "changed" it. This is
    the one place this module does more than a plain accepted/untouched
    branch - see build_reviewed_extracted()'s own comment.
  - Reuses ocr_pipeline.populate_workbook()/clean_number() and
    demo_source.build_demo_workbook() exactly as ocr_upload.py itself
    already does - no extraction or merge logic is duplicated here.
"""

import os
from datetime import datetime

import ocr_pipeline
import ocr_upload
from demo_source import build_demo_workbook


def needs_reviewed_workbook(extracted, approved_corrections):
    """True if the reviewed dataset would differ from the raw one in ANY
    way - either an "accepted" decision, or ANY decision at all
    (including "rejected"/"kept_original") on a field that started out
    "AI_MAPPED", since that field's raw `extracted` already contains the
    AI's guess and must be reverted even when rejected - see
    build_reviewed_extracted()'s own AI_MAPPED reversion rule below."""
    for field, decision in (approved_corrections or {}).items():
        if decision.get("decision") == "accepted":
            return True
        if (extracted or {}).get(field, {}).get("validation_status") == "AI_MAPPED":
            return True
    return False


def build_reviewed_extracted(extracted, approved_corrections):
    """Returns a NEW dict - `extracted` itself is never mutated. Every
    field starts as an exact copy of the original deterministic record.

    "accepted": fy_current/fy_previous/ambiguous/validation_status are
    overridden using the proposal's own already-verified proposed
    values (see ai_correction_proposals.py / ai_dynamic_mapping.py -
    only an already-verified proposal is ever offered for acceptance in
    the UI, never a rejected/hallucinated one), marked
    "HUMAN_APPROVED_CORRECTION" regardless of which module originated
    the proposal.

    "rejected"/"kept_original" on a field that was deterministically OK/
    REVIEW_REQUIRED: a correct no-op - the pre-existing deterministic
    value already sitting in `extracted[field]` is exactly what should
    keep flowing.

    "rejected"/"kept_original" on a field that started out "AI_MAPPED":
    reverted to a genuine NOT_FOUND state - `extracted[field]` already
    contains the AI's populated guess by the time a human reviews it,
    so a plain no-op would silently let a REJECTED guess keep flowing
    downstream just because nothing "changed" it."""

    reviewed = {field: dict(field_data) for field, field_data in (extracted or {}).items()}

    for field, decision in (approved_corrections or {}).items():
        if field not in reviewed:
            continue

        outcome = decision.get("decision")
        was_ai_mapped = (extracted or {}).get(field, {}).get("validation_status") == "AI_MAPPED"

        if outcome == "accepted":
            new_current = ocr_pipeline.clean_number(decision.get("proposed_value_current"))
            new_previous = ocr_pipeline.clean_number(decision.get("proposed_value_previous"))
            # Never let an acceptance replace an existing figure with
            # nothing (e.g. a value-less proposal accepted before
            # ai_correction_proposals' check D existed) - such a record is
            # ignored and the original values keep flowing.
            if ((new_current is None and reviewed[field].get("fy_current") is not None)
                    or (new_previous is None and reviewed[field].get("fy_previous") is not None)):
                continue
            reviewed[field]["fy_current"] = new_current
            reviewed[field]["fy_previous"] = new_previous
            reviewed[field]["ambiguous"] = False
            reviewed[field]["validation_status"] = "HUMAN_APPROVED_CORRECTION"
            reviewed[field]["source_line_number"] = decision.get("proposed_source_line_number")
            reviewed[field]["source_text"] = decision.get("proposed_source_text")
        elif was_ai_mapped:
            reviewed[field]["fy_current"] = None
            reviewed[field]["fy_previous"] = None
            reviewed[field]["ambiguous"] = False
            reviewed[field]["validation_status"] = "NOT_FOUND"
            reviewed[field]["source_line_number"] = None
            reviewed[field]["source_text"] = None

    return reviewed


def regenerate_reviewed_workbook(extracted, approved_corrections):
    """Returns a new merged-workbook path reflecting every reviewed
    decision, or None if nothing would actually change (see
    needs_reviewed_workbook()) - in which case the caller should keep
    using the original merged workbook. Mirrors ocr_upload.py's own
    populate_workbook -> anonymize -> build_demo_workbook sequence
    exactly, just built from the reviewed dict instead of the raw one -
    so the result is indistinguishable in shape from the original
    PDF-mode pipeline's own output."""

    if not needs_reviewed_workbook(extracted, approved_corrections):
        return None

    reviewed_extracted = build_reviewed_extracted(extracted, approved_corrections)

    ocr_upload._ensure_work_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    reviewed_xlsx_path = os.path.join(ocr_upload.UPLOAD_WORK_DIR, f"reviewed_{timestamp}.xlsx")

    wb = ocr_pipeline.populate_workbook(ocr_pipeline.TEMPLATE_FILE, reviewed_extracted)
    wb.save(reviewed_xlsx_path)
    ocr_upload._anonymize_header(reviewed_xlsx_path)

    return build_demo_workbook(extracted_sheet_path=reviewed_xlsx_path)
