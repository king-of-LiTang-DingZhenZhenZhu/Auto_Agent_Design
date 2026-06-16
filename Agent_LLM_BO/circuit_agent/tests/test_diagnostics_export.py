from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from diagnostics_export import export_diagnostics, inject_diagnostic_saves
from topologies import get_topology


class DiagnosticsExportTest(unittest.TestCase):
    def test_injects_explicit_mos_node_saves_into_ac_testbench(self):
        topo = get_topology("5t_ota")
        circuit = topo.generate_circuit()
        testbench = topo.generate_testbench(analysis_type="ac")

        rendered = inject_diagnostic_saves(testbench, circuit)

        self.assertIn("// Diagnostic node saves", rendered)
        self.assertIn("Xdut.tail", rendered)
        self.assertIn("Xdut.vbias", rendered)
        self.assertIn("Xdut.lout", rendered)

    def test_exports_ac_and_operating_point_csvs_from_ascii_raw(self):
        source_raw = Path("/Users/hnchen/Downloads/raw")
        if not source_raw.exists():
            self.skipTest("sample raw directory is not available")

        topo = get_topology("two_stage_ota")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            netlist = root / "circuit.cir"
            netlist.write_text(topo.generate_circuit(), encoding="utf-8")

            exported = export_diagnostics(
                raw_dir=source_raw,
                netlist_path=netlist,
                out_dir=root / "diagnostics",
            )

            self.assertIn("ac_response", exported)
            self.assertIn("dc_operating_points", exported)
            with open(exported["ac_response"], newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertGreater(len(rows), 0)
            self.assertIn("frequency_hz", rows[0])
            self.assertIn("magnitude_db", rows[0])

            with open(
                exported["dc_operating_points"], newline="", encoding="utf-8"
            ) as f:
                op_rows = list(csv.DictReader(f))
            self.assertTrue(any(row["instance"] == "Xdut.Mtail" for row in op_rows))
            mtail = next(row for row in op_rows if row["instance"] == "Xdut.Mtail")
            self.assertIn("gm", mtail)
            self.assertIn("gds", mtail)


if __name__ == "__main__":
    unittest.main()
