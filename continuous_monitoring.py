"""
Area 7 - Continuous Monitoring & Early Warning Signals.

Reference workbook dependency (verified from actual formulas):
    '7-Continuous Monitoring'!C4 = '2-Financial Analysis'!E7    (Revenue % change)
    '7-Continuous Monitoring'!C5 = '2-Financial Analysis'!E14   (Total Debt % change)
    '7-Continuous Monitoring'!C6 = '2-Financial Analysis'!E19   (DSCR % change)
    '7-Continuous Monitoring'!D7 references '4-Covenants'!E11:E12 (covenant RAG status)

So this module consumes financial_analysis.py's already-computed %-change
values (the frozen module, via its MetricRow.pct_change fields - not
recomputed) AND covenants.py's already-computed RAG status (module 4,
built earlier in this same implementation pass) - it does not re-read
Input Data or re-derive covenant status independently.

Two triggers (external rating downgrade, adverse news) are explicitly
"Input value" cells in the reference workbook representing a point-in-time
analyst snapshot ("No Change"/"PASS"), not something derivable from any
data this application has access to - there is no external credit-rating
feed or news-monitoring feed anywhere in this project. Shown as not
monitored by this application, with the reference workbook's own snapshot
displayed for context only (not asserted as a live/current fact).

DISCOVERED REFERENCE-WORKBOOK INCONSISTENCY (reproduced faithfully, not
"fixed" - flagged in the UI): D10's Overall Status formula is
=IF(COUNTIF(D4:D9,"NOT PASS")>0,"NOT PASS","PASS"). Every individual
trigger in D4:D9 can only ever read "PASS", "FAIL", or "WATCH LIST" - the
literal text "NOT PASS" can never appear in that range, so this COUNTIF is
structurally always 0 and D10 always evaluates to "PASS", regardless of
whether any individual trigger actually failed. Reproduced exactly (the
Python equivalent likewise always evaluates to "PASS" given the same value
domain) rather than silently changed to check for "FAIL"/"WATCH LIST".

Formulas reproduced exactly as found in '7-Continuous Monitoring':
    D4 = PASS if Revenue %chg > -10% (B4) else FAIL
    D5 = PASS if Total Debt %chg <= 15% (B5) else FAIL
    D6 = PASS if DSCR %chg > -35% (B6) else FAIL
    D7 = WATCH LIST if any covenant AMBER/RED, else PASS
    D8, D9 = analyst snapshot inputs (see note above)
    D10 = Overall Status (see discovered-inconsistency note above)
"""

from dataclasses import dataclass

from openpyxl import load_workbook

CONTINUOUS_MONITORING_SHEET = "7-Continuous Monitoring"


@dataclass(frozen=True)
class MonitoringPolicy:
    revenue_decline_threshold: float
    total_debt_increase_threshold: float
    dscr_deterioration_threshold: float
    external_rating_note: str
    adverse_news_note: str


@dataclass(frozen=True)
class MonitoringTrigger:
    label: str
    threshold_text: str
    computed_value: object
    status: str
    note: str = ""


@dataclass(frozen=True)
class ContinuousMonitoringResult:
    triggers: list
    overall_status: str


def load_monitoring_policy(workbook_path) -> MonitoringPolicy:
    ws = load_workbook(workbook_path, data_only=True)[CONTINUOUS_MONITORING_SHEET]
    return MonitoringPolicy(
        revenue_decline_threshold=float(ws["B4"].value),
        total_debt_increase_threshold=float(ws["B5"].value),
        dscr_deterioration_threshold=float(ws["B6"].value),
        external_rating_note=str(ws["C8"].value),
        adverse_news_note=str(ws["C9"].value),
    )


_NOT_AVAILABLE = "Not Available / Requires Review"


def _pct_trigger(label, threshold_text, pct_change, ok_fn):
    # revenue/total-debt %-change come from core P&L/Balance Sheet fields
    # (virtually always present); dscr %-change depends on the Reported
    # Ratios disclosure, which is not universal (see financial_analysis.py's
    # guard on the same field) - None is shown as not-available rather than
    # crashing on `None > threshold`.
    if pct_change is None:
        return MonitoringTrigger(
            label, threshold_text, None, _NOT_AVAILABLE,
            note="Not available - this document's disclosures do not support computing this trigger.",
        )
    return MonitoringTrigger(label, threshold_text, pct_change, "PASS" if ok_fn(pct_change) else "FAIL")


def build_continuous_monitoring(policy: MonitoringPolicy, revenue_pct_change, total_debt_pct_change,
                                 dscr_pct_change, covenant_rag_breach: bool) -> ContinuousMonitoringResult:
    triggers = []

    triggers.append(_pct_trigger(
        "Revenue decline YoY", f"> {policy.revenue_decline_threshold:.0%}",
        revenue_pct_change, lambda v: v > policy.revenue_decline_threshold,
    ))
    triggers.append(_pct_trigger(
        "Total debt increase YoY", f"<= {policy.total_debt_increase_threshold:.0%}",
        total_debt_pct_change, lambda v: v <= policy.total_debt_increase_threshold,
    ))
    triggers.append(_pct_trigger(
        "Reported DSCR deterioration YoY", f"> {policy.dscr_deterioration_threshold:.0%}",
        dscr_pct_change, lambda v: v > policy.dscr_deterioration_threshold,
    ))
    triggers.append(MonitoringTrigger(
        "Any covenant on Amber/Red", "0 breaches",
        "breach" if covenant_rag_breach else "no breach",
        "WATCH LIST" if covenant_rag_breach else "PASS",
    ))
    triggers.append(MonitoringTrigger(
        "External rating downgrade", "Any downgrade -> watch list",
        None, "NOT MONITORED",
        note=f"Reference workbook snapshot (not live-monitored by this application): {policy.external_rating_note}",
    ))
    triggers.append(MonitoringTrigger(
        "Adverse news / qualitative disclosure", "Material adverse item -> Amber, analyst verifies",
        None, "NOT MONITORED",
        note=f"Reference workbook snapshot (not live-monitored by this application): {policy.adverse_news_note}",
    ))

    # '7-Continuous Monitoring'!D10 - see discovered-inconsistency note in
    # the module docstring: only the literal text "NOT PASS" would trip
    # this gate, and no trigger status in this workbook can ever be that
    # text, so it structurally always resolves to "PASS".
    statuses = [t.status for t in triggers]
    overall_status = "NOT PASS" if statuses.count("NOT PASS") > 0 else "PASS"

    return ContinuousMonitoringResult(triggers=triggers, overall_status=overall_status)
