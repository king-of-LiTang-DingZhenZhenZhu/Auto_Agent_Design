from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from analogskills.imported_design.adapters import PhysicalAdapterRequired, adapt_topology
from analogskills.imported_design.handoff import build_imported_design_handoff
from analogskills.imported_design.schema import ImportedDesignHandoff
from topologies import get_topology
from virtuoso_export.parser import parse_netlist


class HandoffAdapterTest(unittest.TestCase):
    def _build(self, topology: str) -> ImportedDesignHandoff:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / topology
            project.mkdir()
            netlist = project / "circuit.cir"
            netlist.write_text(get_topology(topology).generate_circuit(), encoding="utf-8")
            pvt = project / "pvt" / "pvt_results.json"
            pvt.parent.mkdir()
            pvt.write_text(json.dumps({"pvt_pass": True, "summary": {"total_corners": 27}}), encoding="utf-8")
            handoff = build_imported_design_handoff(
                project_dir=project,
                topology=topology,
                final_netlist=netlist,
                final_source="bo_best",
                pvt_results=pvt,
                schematic_ir=parse_netlist(netlist),
            )
            roundtrip = project / "physical" / "handoff.json"
            handoff.write_json(roundtrip)
            return ImportedDesignHandoff.read_json(roundtrip)

    def test_two_stage_roundtrip_and_si_values(self):
        handoff = self._build("two_stage_ota")
        self.assertEqual(len(handoff.devices), 10)
        mdiff = next(item for item in handoff.devices if item.name == "Mdiff1")
        self.assertAlmostEqual(mdiff.parameters["W"], 10e-6)
        self.assertAlmostEqual(mdiff.parameters["L"], 60e-9)
        self.assertEqual(mdiff.parameters["nf"], 1)

    def test_strongarm_exact_frontend_connectivity(self):
        handoff = self._build("strongarm_latch")
        self.assertEqual(len(handoff.devices), 11)
        s1 = next(item for item in handoff.devices if item.name == "S1")
        self.assertEqual(s1.nodes, ("p", "clk", "vdd", "vdd"))

    def test_unknown_or_modified_topology_is_rejected(self):
        ir = parse_netlist(get_topology("strongarm_latch").generate_circuit())
        with self.assertRaises(PhysicalAdapterRequired):
            adapt_topology("backend_strongarm", ir.instances, ir.ports)
        with self.assertRaises(PhysicalAdapterRequired):
            adapt_topology("strongarm_latch", ir.instances[:-1], ir.ports)


if __name__ == "__main__":
    unittest.main()
