from __future__ import annotations

import unittest
from pathlib import Path

from schematic_review import (
    load_schematic_spec,
    render_schematic_svg,
    validate_netlist_connectivity,
)
from topologies import get_topology


REPO_ROOT = Path(__file__).resolve().parents[3]
STRONGARM_ROOT = (
    REPO_ROOT
    / "knowledge_base"
    / "Comparator_knowledge_base"
    / "topologies"
    / "schematics"
)
BANBA_ROOT = (
    REPO_ROOT
    / "knowledge_base"
    / "Bandgap_knowledge_base"
    / "topologies"
    / "schematics"
)


class SchematicReviewTest(unittest.TestCase):
    def _assert_topology_matches_review(
        self,
        topology_name: str,
        root: Path,
        stem: str,
    ) -> dict:
        spec = load_schematic_spec(root / f"{stem}_connectivity.json")
        netlist = get_topology(topology_name).generate_circuit()

        self.assertEqual(validate_netlist_connectivity(netlist, spec), [])
        self.assertEqual(
            (root / f"{stem}_schematic.svg").read_text(encoding="utf-8"),
            render_schematic_svg(spec),
        )
        return spec

    def test_strongarm_netlist_matches_reviewed_connectivity(self):
        spec = self._assert_topology_matches_review(
            "strongarm_latch",
            STRONGARM_ROOT,
            "strongarm_latch",
        )

        self.assertEqual(
            spec["paper"]["figure"],
            "Figure 1(b), modified StrongARM latch",
        )
        self.assertEqual(
            spec["polarity"]["result"],
            "outn (X) resolves low; outp (Y) resolves high",
        )

    def test_banba_netlist_matches_reviewed_connectivity(self):
        spec = self._assert_topology_matches_review(
            "banba_sub1v_bandgap",
            BANBA_ROOT,
            "banba_sub1v_bandgap",
        )

        self.assertEqual(
            spec["relations"]["diode_area_ratio"],
            "QN / Q1 = 8",
        )
        self.assertEqual(len(spec["implementation_deviations"]), 3)

    def test_connectivity_mismatch_is_reported(self):
        spec = load_schematic_spec(
            STRONGARM_ROOT / "strongarm_latch_connectivity.json"
        )
        netlist = get_topology("strongarm_latch").generate_circuit().replace(
            "M1 (p vip ntail vss)",
            "M1 (q vip ntail vss)",
        )

        errors = validate_netlist_connectivity(netlist, spec)

        self.assertTrue(any("M1 nets differ" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
