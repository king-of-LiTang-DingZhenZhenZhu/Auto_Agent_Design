from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analogskills.eda.oa import OaCellView, OaPath, OaRect, OaVia, OaWritePlan
from analogskills.layout.physical import analyze_plan_design_rules, via_landing_bboxes
from analogskills.pdk import resolve_pdk_config


class LocalDesignRuleTest(unittest.TestCase):
    def setUp(self):
        self.pdk = resolve_pdk_config("crn28hpcp")
        self.cellview = OaCellView("work", "drc_probe", "layout", "maskLayout")

    def test_legal_basic_metal_geometry_passes(self):
        plan = OaWritePlan(
            self.cellview,
            nets=("a", "b"),
            rects=(
                OaRect("M1", "drawing", (0.0, 0.0, 0.2, 0.2), "a"),
                OaRect("M1", "drawing", (0.3, 0.0, 0.5, 0.2), "b"),
            ),
        )
        report = analyze_plan_design_rules(plan, self.pdk)
        self.assertTrue(report["passed"])
        self.assertEqual(report["rule_issues"], [])

    def test_min_width_violation_is_rejected(self):
        plan = OaWritePlan(
            self.cellview,
            nets=("a",),
            paths=(OaPath("M1", "drawing", ((0.0, 0.0), (0.5, 0.0)), 0.04, "a"),),
        )
        report = analyze_plan_design_rules(plan, self.pdk)
        self.assertFalse(report["passed"])
        self.assertIn("min_width", {row["rule"] for row in report["rule_issues"]})

    def test_min_area_violation_is_rejected(self):
        plan = OaWritePlan(
            self.cellview,
            nets=("a",),
            rects=(OaRect("M3", "drawing", (0.0, 0.0, 0.05, 0.05), "a"),),
        )
        report = analyze_plan_design_rules(plan, self.pdk)
        self.assertFalse(report["passed"])
        self.assertIn("min_area", {row["rule"] for row in report["rule_issues"]})

    def test_different_net_spacing_violation_is_rejected(self):
        plan = OaWritePlan(
            self.cellview,
            nets=("a", "b"),
            rects=(
                OaRect("M2", "drawing", (0.0, 0.0, 0.2, 0.2), "a"),
                OaRect("M2", "drawing", (0.24, 0.0, 0.44, 0.2), "b"),
            ),
        )
        report = analyze_plan_design_rules(plan, self.pdk)
        self.assertFalse(report["passed"])
        self.assertIn("min_spacing", {row["rule"] for row in report["rule_issues"]})

    def test_missing_via_landing_is_rejected(self):
        plan = OaWritePlan(
            self.cellview,
            nets=("a",),
            vias=(OaVia("VIA1", (0.0, 0.0), "a"),),
        )
        report = analyze_plan_design_rules(plan, self.pdk)
        self.assertFalse(report["passed"])
        self.assertTrue(report["via_landing_issues"])

    def test_adjacent_same_net_shapes_can_jointly_cover_via_landing(self):
        via = OaVia("VIA1", (0.0, 0.0), "a")
        rects = []
        for layer, bbox in via_landing_bboxes(via, self.pdk):
            center_x = 0.5 * (bbox[0] + bbox[2])
            rects.extend(
                (
                    OaRect(layer, "drawing", (bbox[0], bbox[1], center_x, bbox[3]), "a"),
                    OaRect(layer, "drawing", (center_x, bbox[1], bbox[2], bbox[3]), "a"),
                )
            )
        plan = OaWritePlan(self.cellview, nets=("a",), rects=tuple(rects), vias=(via,))
        report = analyze_plan_design_rules(plan, self.pdk)
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
