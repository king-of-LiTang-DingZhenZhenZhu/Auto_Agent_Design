from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from analogskills.imported_design import (
    build_imported_design_handoff,
    import_prepared_schematic,
    prepare_imported_schematic,
)
from topologies import get_topology
from virtuoso_export.parser import parse_netlist


class ImportedSchematicTest(unittest.TestCase):
    def test_import_records_successful_cached_oa_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "two_stage"
            project.mkdir()
            netlist = project / "circuit.cir"
            netlist.write_text(get_topology("two_stage_ota").generate_circuit(), encoding="utf-8")
            pvt = project / "pvt" / "pvt_results.json"
            pvt.parent.mkdir()
            pvt.write_text(json.dumps({"pvt_pass": True}), encoding="utf-8")
            output = project / "schematic"
            handoff = build_imported_design_handoff(
                project_dir=project,
                topology="two_stage_ota",
                final_netlist=netlist,
                final_source="bo_best",
                pvt_results=pvt,
                schematic_ir=parse_netlist(netlist),
                output_dir=output,
            )
            prepared = prepare_imported_schematic(handoff, output_root=output)
            completed = {"name": "schematic_oa", "ok": True, "executor": "skill_server"}

            with patch(
                "analogskills.imported_design.flow._schematic_preflight",
                return_value={"virtuoso": "virtuoso", "pdk_lib": "/pdk"},
            ), patch(
                "analogskills.imported_design.flow._run_cached_oa_stage",
                return_value=completed,
            ) as run:
                imported = import_prepared_schematic(prepared)

            self.assertTrue(imported.imported)
            self.assertEqual(imported.status, "imported")
            self.assertEqual(run.call_count, 1)
            manifest = json.loads((output / "schematic_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "imported")
            self.assertEqual(manifest["runs"][0]["executor"], "skill_server")


if __name__ == "__main__":
    unittest.main()
