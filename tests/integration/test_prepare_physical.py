from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from analogskills.imported_design import build_imported_design_handoff, prepare_imported_physical_run
from topologies import get_topology
from virtuoso_export.parser import parse_netlist


class PreparePhysicalIntegrationTest(unittest.TestCase):
    def test_both_supported_topologies_prepare_without_upstream_repo(self):
        for topology in ("two_stage_ota", "strongarm_latch"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp) / topology
                project.mkdir()
                netlist = project / "circuit.cir"
                netlist.write_text(get_topology(topology).generate_circuit(), encoding="utf-8")
                pvt = project / "pvt" / "pvt_results.json"
                pvt.parent.mkdir()
                pvt.write_text(json.dumps({"pvt_pass": True, "summary": {"total_corners": 27}}), encoding="utf-8")
                handoff = build_imported_design_handoff(
                    project_dir=project, topology=topology, final_netlist=netlist,
                    final_source="bo_best", pvt_results=pvt, schematic_ir=parse_netlist(netlist),
                )
                result = prepare_imported_physical_run(handoff)
                self.assertEqual(result.status, "prepared")
                self.assertTrue(Path(result.layout_plan_path).is_file())
                self.assertTrue(Path(result.layout_skill_path).is_file())
                self.assertTrue(Path(result.lvs_source_path).is_file())
                text = Path(result.layout_skill_path).read_text(encoding="utf-8")
                self.assertNotIn("drawn_primitive", text)
                self.assertIn('ddCreateLib("BO_Designs")', text)
                self.assertIn('techBindTechFile(libObj "tsmcN28")', text)
                self.assertLess(text.index("techBindTechFile"), text.index("dbOpenCellViewByType"))
                self.assertTrue(text.rstrip().endswith("exit()"))
                streamout = Path(result.physical_root) / "oa" / "streamout.il"
                self.assertTrue(streamout.read_text(encoding="utf-8").rstrip().endswith("exit()"))
                mapping = json.loads((Path(result.physical_root) / "instance_mapping.json").read_text(encoding="utf-8"))
                self.assertEqual(set(mapping), {row.name for row in handoff.devices})
                if topology == "two_stage_ota":
                    self.assertEqual(len(mapping["Mtail"]["lvs_instances"]), 2)
                    self.assertEqual(len(mapping["Mload"]["lvs_instances"]), 4)


if __name__ == "__main__":
    unittest.main()
