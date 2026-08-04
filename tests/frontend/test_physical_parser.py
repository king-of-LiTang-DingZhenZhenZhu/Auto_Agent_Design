from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path.insert(0, str(CIRCUIT_AGENT))

from topologies import get_topology
from virtuoso_export.parser import parse_netlist


class PhysicalParserTest(unittest.TestCase):
    def test_current_two_stage_signature(self):
        ir = parse_netlist(get_topology("two_stage_ota").generate_circuit())
        self.assertEqual(len(ir.instances), 10)
        self.assertEqual(sum(row.kind == "mos" for row in ir.instances), 8)
        self.assertEqual({row.name for row in ir.instances if row.kind != "mos"}, {"Rz", "Cc"})

    def test_strongarm_keeps_precharge_mos_with_s_names(self):
        ir = parse_netlist(get_topology("strongarm_latch").generate_circuit())
        self.assertEqual(len(ir.instances), 11)
        self.assertEqual(sum(row.kind == "mos" for row in ir.instances), 11)
        self.assertTrue({"S1", "S2", "S3", "S4"}.issubset({row.name for row in ir.instances}))


if __name__ == "__main__":
    unittest.main()
