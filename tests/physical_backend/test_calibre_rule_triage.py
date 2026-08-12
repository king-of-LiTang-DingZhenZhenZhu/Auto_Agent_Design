from __future__ import annotations

from dataclasses import dataclass, field
import unittest

from analogskills.pdk import resolve_pdk_config
from analogskills.repair.calibre_closure import (
    classify_calibre_rule_for_triage,
    summarize_calibre_rule_triage,
)
from analogskills.repair.calibre_eco_closure import build_next_calibre_eco_closure_patch
from analogskills.repair.calibre_eco_closure import run_calibre_eco_closure_loop


@dataclass(frozen=True)
class _Result:
    rule: str
    message: str = ""
    properties: dict[str, object] = field(default_factory=dict)


class CalibreRuleTriageTest(unittest.TestCase):
    def setUp(self):
        self.pdk = resolve_pdk_config("crn28hpcp")

    def test_classifies_configured_dummy_and_marker_rules(self):
        self.assertEqual(classify_calibre_rule_for_triage(_Result("PO.W.18"), pdk=self.pdk).domain, "pcell_dummy")
        self.assertEqual(classify_calibre_rule_for_triage(_Result("DOD.R.1"), pdk=self.pdk).domain, "dummy_marker")

    def test_metal_spacing_needs_access_context(self):
        generic = classify_calibre_rule_for_triage(_Result("M2.S.1"), pdk=self.pdk)
        access = classify_calibre_rule_for_triage(
            _Result("M2.S.1", message="spacing at pcell_access terminal_access landing"),
            pdk=self.pdk,
        )
        self.assertEqual(generic.domain, "routing")
        self.assertEqual(access.domain, "terminal_access")

    def test_summary_aggregates_rule_ids_and_orders_priority(self):
        summary = summarize_calibre_rule_triage(
            (_Result("M2.S.1"), _Result("PO.W.18"), _Result("PO.W.18"), _Result("DPO.R.1")),
            pdk=self.pdk,
        )
        self.assertEqual(summary["priority_blocking_count"], 3)
        self.assertTrue(summary["routing_eco_blocked"])
        self.assertEqual(summary["repair_queue"][0]["priority"], 0)
        po = next(row for row in summary["repair_queue"] if row["rule"] == "PO.W.18")
        self.assertEqual(po["count"], 2)
        self.assertEqual(
            po["parameters"],
            ["dummyPolyWidth", "dummyPolyWidth2", "secondDummyPolyWidth"],
        )

    def test_pcell_marker_blocks_routing_eco(self):
        decision = build_next_calibre_eco_closure_patch(
            object(),
            (_Result("PO.W.18"), _Result("M2.S.1")),
            pdk=self.pdk,
        )
        self.assertFalse(decision.patch_available)
        self.assertEqual(decision.reason, "pcell_dummy_or_access_markers_must_close_before_routing_eco")

        closure = run_calibre_eco_closure_loop(object(), (_Result("PO.W.18"),), pdk=self.pdk)
        self.assertFalse(closure.converged)
        self.assertEqual(closure.reason, decision.reason)


if __name__ == "__main__":
    unittest.main()
