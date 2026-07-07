from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models import SimResult
from pdk_profiles import get_pdk_profile
from pvt_simulation import (
    PVTCorner,
    default_pvt_corners,
    patch_netlist_for_corner,
    patch_testbench_for_corner,
    run_pvt_verification,
    summarize_pvt,
)
from topologies import get_topology


class PVTSimulationTests(unittest.TestCase):
    def test_default_pvt_matrix_has_27_stable_corners(self):
        corners = default_pvt_corners(get_pdk_profile())

        self.assertEqual(len(corners), 27)
        self.assertEqual(len({corner.corner_id for corner in corners}), 27)
        self.assertEqual(corners[0].corner_id, "tt_vmin0p9_tm40")
        self.assertEqual(corners[-1].corner_id, "ff_vmax1p1_t125")
        self.assertEqual(
            {corner.process for corner in corners},
            {"tt", "ss", "ff"},
        )

    def test_patch_netlist_replaces_process_section(self):
        corner = PVTCorner("ss", "top_ss", "vmin", 0.9, 27)
        netlist = """
simulator lang=spectre insensitive=yes
include "/old/path/toplevel.scs" section=top_tt
subckt tiny in out vdd vss
ends tiny
"""

        patched = patch_netlist_for_corner(netlist, corner, get_pdk_profile())

        self.assertIn('include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_ss', patched)
        self.assertNotIn("section=top_tt", patched)

    def test_patch_netlist_normalizes_pdk_include_path(self):
        corner = PVTCorner("ss", "top_ss", "vmin", 0.9, 27)
        netlist = """
simulator lang=spectre insensitive=yes
include "old.scs" section=top_tt
subckt tiny in out vdd vss
ends tiny
"""
        with patch.dict(
            "os.environ",
            {"PDK_SPECTRE_PATH": "PDKS/TSMC28nm/models/spectre/toplevel.scs"},
        ):
            patched = patch_netlist_for_corner(netlist, corner)

        self.assertIn(
            'include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_ss',
            patched,
        )
        self.assertNotIn('include "PDKS/TSMC28nm', patched)

    def test_patch_testbench_replaces_vdd_and_temperature(self):
        corner = PVTCorner("ff", "top_ff", "vmax", 1.1, 125)
        tb = """
include "circuit.cir"
parameters VDD=0.9 VCM=0.3 CL=1p
tempOption options temp=27
"""

        patched = patch_testbench_for_corner(tb, corner)

        self.assertIn("parameters VDD=1.1 VCM=0.3 CL=1p", patched)
        self.assertIn("tempOption options temp=125", patched)

    def test_summarize_pvt_reports_failures_and_worst_metrics(self):
        rows = [
            {
                "corner_id": "tt",
                "all_targets_met": True,
                "gain_db(dB)": "60.00",
                "gbw_hz(MHz)": "100.00",
                "phase_margin_deg(deg)": "65.00",
                "power_w(mW)": "0.500",
                "slew_rate_v_per_s(V/us)": "120.00",
                "settling_time_s(ns)": "10.00",
            },
            {
                "corner_id": "ss",
                "all_targets_met": False,
                "gain_db(dB)": "50.00",
                "gbw_hz(MHz)": "80.00",
                "phase_margin_deg(deg)": "55.00",
                "power_w(mW)": "0.700",
                "slew_rate_v_per_s(V/us)": "90.00",
                "settling_time_s(ns)": "18.00",
            },
        ]

        summary = summarize_pvt(rows)

        self.assertFalse(summary["pvt_pass"])
        self.assertEqual(summary["failed_corner_ids"], ["ss"])
        self.assertEqual(summary["worst"]["min_gain_db"]["corner_id"], "ss")
        self.assertEqual(summary["worst"]["max_power_mw"]["corner_id"], "ss")

    def test_run_pvt_dry_run_uses_review_candidate_and_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            bo_netlist = project / "netlist" / "circuit.cir"
            bo_netlist.parent.mkdir()
            bo_netlist.write_text(
                get_topology("5t_ota").generate_circuit(),
                encoding="utf-8",
            )
            sim_dir = project / "simulation"
            sim_dir.mkdir()
            (sim_dir / "tb_circuit.scs").write_text(
                get_topology("5t_ota").generate_testbench(analysis_type="ac"),
                encoding="utf-8",
            )
            candidate_dir = project / "agent_review" / "candidates" / "iter_000_candidate_01"
            candidate_dir.mkdir(parents=True)
            candidate_netlist = candidate_dir / "circuit.cir"
            candidate_netlist.write_text(
                get_topology("folded_cascode").generate_circuit(),
                encoding="utf-8",
            )
            (candidate_dir / "tb.scs").write_text(
                get_topology("folded_cascode").generate_testbench(analysis_type="ac"),
                encoding="utf-8",
            )
            self._write_candidate_metrics(
                project / "agent_review" / "candidate_metrics.csv",
                candidate_dir,
            )
            (project / "optimization_log.json").write_text(
                json.dumps({"targets": {"gain_db": 40, "bandwidth_hz": 1e6}}),
                encoding="utf-8",
            )
            (project / "results.json").write_text(
                json.dumps({
                    "project_name": "proj",
                    "all_targets_met": True,
                    "netlist_file": str(bo_netlist),
                }),
                encoding="utf-8",
            )

            report = run_pvt_verification(
                results_path=project / "results.json",
                simulate=False,
                dry_run=True,
            )

            self.assertEqual(report["source"], "agent_review")
            self.assertEqual(report["corners"], 27)
            self.assertTrue((project / "pvt" / "pvt_results.csv").exists())
            self.assertTrue((project / "pvt" / "pvt_results.json").exists())
            self.assertTrue((project / "pvt" / "pvt_report.md").exists())
            corner = project / "pvt" / "corners" / "tt_vmin0p9_tm40"
            self.assertTrue((corner / "circuit.cir").exists())
            self.assertTrue((corner / "tb.scs").exists())
            with (project / "pvt" / "pvt_results.csv").open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 27)

    def _write_candidate_metrics(self, path: Path, candidate_dir: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "candidate_path",
            "gain_db(dB)",
            "gbw_hz(MHz)",
            "phase_margin_deg(deg)",
            "power_w(mW)",
            "slew_rate_v_per_s(V/us)",
            "settling_time_s(ns)",
            "error_message",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({
                "candidate_path": str(candidate_dir),
                "gain_db(dB)": "60",
                "gbw_hz(MHz)": "100",
                "phase_margin_deg(deg)": "70",
                "power_w(mW)": "0.5",
                "slew_rate_v_per_s(V/us)": "",
                "settling_time_s(ns)": "",
                "error_message": "",
            })


if __name__ == "__main__":
    unittest.main()
