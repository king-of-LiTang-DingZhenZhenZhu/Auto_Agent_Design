from __future__ import annotations

import csv
import math
import tempfile
import unittest
from pathlib import Path

from diagnostics_export import (
    export_diagnostics,
    inject_diagnostic_saves,
    write_diagnostics_summary,
)
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

    def test_exports_uppercase_nan_without_shifting_operating_point_fields(self):
        info_text = """\
TYPE
"bsim4" STRUCT(
"ids" FLOAT DOUBLE PROP(
)
"vgs" FLOAT DOUBLE PROP(
)
"vds" FLOAT DOUBLE PROP(
)
"vth" FLOAT DOUBLE PROP(
)
"vdsat" FLOAT DOUBLE PROP(
)
"gm" FLOAT DOUBLE PROP(
)
) PROP(
"key" "inst"
)
VALUE
"Xdut.M1" "bsim4" (
1e-5
NaN
0.3
0.4
0.2
2e-4
) PROP(
"model" "nch"
)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            (raw / "op1.info").write_text(info_text, encoding="utf-8")
            netlist = root / "circuit.cir"
            netlist.write_text("M1 (d g s b) nch\n", encoding="utf-8")

            exported = export_diagnostics(
                raw_dir=raw,
                netlist_path=netlist,
                out_dir=root / "diagnostics",
            )

            self.assertIn("dc_operating_points", exported)
            with open(
                exported["dc_operating_points"], newline="", encoding="utf-8"
            ) as f:
                row = next(csv.DictReader(f))
            self.assertTrue(math.isnan(float(row["vgs"])))
            self.assertEqual(float(row["vds"]), 0.3)
            self.assertEqual(float(row["vth"]), 0.4)
            self.assertEqual(float(row["vdsat"]), 0.2)
            self.assertEqual(float(row["gm"]), 2e-4)

    def test_writes_readable_diagnostics_summary_from_csvs(self):
        with tempfile.TemporaryDirectory() as tmp:
            diagnostics = Path(tmp)
            (diagnostics / "dc_operating_points.csv").write_text(
                "\n".join([
                    "instance,model,vd,vg,vs,id,ids,gm,gds,vgs,vds,vth,vdsat,gmoverid",
                    "Xdut.M1,nch_mac,0.4,0.7,0,1e-5,1e-5,2e-4,1e-6,0.7,0.05,0.4,0.12,20",
                    "Xdut.M2,pch_mac,0.8,0.3,1,-2e-5,-2e-5,3e-4,2e-6,-0.7,-0.3,-0.4,-0.15,15",
                ]),
                encoding="utf-8",
            )
            (diagnostics / "ac_response.csv").write_text(
                "\n".join([
                    "frequency_hz,vout_real,vout_imag,magnitude_v,magnitude_db,phase_deg",
                    "1000,100,0,100,40,-1",
                    "1000000,1,0,1,0,-120",
                    "10000000,0.1,0,0.1,-20,-170",
                ]),
                encoding="utf-8",
            )

            summary = write_diagnostics_summary(diagnostics)

            self.assertIsNotNone(summary)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("DC Operating Points", text)
            self.assertIn("model", text)
            self.assertIn("vgs(V)", text)
            self.assertIn("vth(V)", text)
            self.assertIn("vod(V)", text)
            self.assertIn("id(uA)", text)
            self.assertIn("ro(kOhm)", text)
            self.assertIn("|vds|-|vdsat|(V)", text)
            self.assertIn("nch_mac", text)
            self.assertIn("10.00", text)
            self.assertIn("1000.00", text)
            self.assertIn("0.15", text)
            self.assertNotIn("gds(uS)", text)
            self.assertNotIn("vds-vdsat", text)
            self.assertNotIn("10.000 uA", text)
            self.assertIn("linear/warning", text)
            self.assertIn("AC Response", text)
            self.assertIn("Unity-gain frequency: 1.00 MHz", text)
            self.assertIn("Estimated phase margin: 60.00 deg", text)


if __name__ == "__main__":
    unittest.main()
