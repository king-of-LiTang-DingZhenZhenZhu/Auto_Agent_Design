from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from topologies import get_topology
from virtuoso_export.exporter import export_from_results, prepare_virtuoso_workspace
from virtuoso_export.models import DEFAULT_DEVICE_MAP
from virtuoso_export.parser import parse_netlist
from virtuoso_export.skill_writer import write_skill


class VirtuosoExportTest(unittest.TestCase):
    def test_parse_folded_cascode_instances_and_ports(self):
        netlist = get_topology("folded_cascode").generate_circuit()

        ir = parse_netlist(netlist)

        self.assertEqual(ir.subckt_name, "folded_cascode")
        self.assertEqual(ir.ports, ["vip", "vin", "vout", "ibias", "vdd", "vss"])

        mos_instances = [inst for inst in ir.instances if inst.kind == "mos"]
        self.assertEqual(len(mos_instances), 28)

        mtailp = next(inst for inst in ir.instances if inst.name == "Mtailp")
        self.assertEqual(mtailp.model, "pch_lvt_mac")
        self.assertEqual(mtailp.nodes, ["ntail", "VB1", "vdd", "vdd"])
        self.assertEqual(mtailp.params["W"], "4.8u")
        self.assertEqual(mtailp.params["L"], "400n")
        self.assertEqual(mtailp.params["nf"], "4")
        self.assertEqual(mtailp.params["m"], "4")

    def test_parse_resistor_and_capacitor(self):
        netlist = get_topology("folded_cascode").generate_circuit()

        ir = parse_netlist(netlist)

        rz = next(inst for inst in ir.instances if inst.name == "Rz")
        cc = next(inst for inst in ir.instances if inst.name == "Cc")
        self.assertEqual(rz.kind, "res")
        self.assertEqual(rz.nodes, ["nstage1", "n_rz"])
        self.assertEqual(rz.params["R"], "1k")
        self.assertEqual(cc.kind, "cap")
        self.assertEqual(cc.nodes, ["n_rz", "vout"])
        self.assertEqual(cc.params["C"], "250f")

    def test_skill_writer_contains_target_and_instances(self):
        ir = parse_netlist(get_topology("5t_ota").generate_circuit())

        skill = write_skill(
            ir,
            DEFAULT_DEVICE_MAP,
            lib_name="BO_Designs",
            cell_name="ota_5t_opt",
        )

        self.assertIn('libName = "BO_Designs"', skill)
        self.assertIn('cellName = "ota_5t_opt"', skill)
        self.assertIn('dbCreateInst(cv master "Mtail"', skill)
        self.assertIn('dbCreateInst(cv master "Mdp1"', skill)
        self.assertIn("boCreateNetStub", skill)
        self.assertIn("boMaybeCreateLabel", skill)
        for port in ["vip", "vin", "vout", "vbias", "vdd", "vss"]:
            self.assertIn(f'dbCreateTerm(net "{port}"', skill)

    def test_skill_writer_preserves_m_and_uses_compact_coordinates(self):
        ir = parse_netlist(
            """
simulator lang=spectre
parameters Wtail=12u Ltail=200n
subckt tiny vip vin vout vdd vss
Mtail (vout vin vss vss) nch_mac w=12u l=200n nf=5 m=4
ends tiny
"""
        )

        skill = write_skill(
            ir,
            DEFAULT_DEVICE_MAP,
            lib_name="BO_Designs",
            cell_name="tiny_opt",
        )

        self.assertIn('boReplaceProp(inst "m"', skill)
        self.assertIn('dbCreateProp(obj name "int" atoi(value))', skill)
        self.assertIn('boReplaceProp(inst "nf" "5")', skill)
        self.assertIn('boReplaceProp(inst "m" "4")', skill)
        self.assertIn('dbCreateInst(cv master "Mtail" list(-7.5 3.0) "R0")', skill)
        self.assertIn('boCreateNetStub(cv "vout"', skill)

    def test_missing_device_map_fails_before_writing_skill(self):
        ir = parse_netlist(get_topology("5t_ota").generate_circuit())
        incomplete_map = {"res": DEFAULT_DEVICE_MAP["res"]}

        with self.assertRaisesRegex(ValueError, "Device map is missing"):
            write_skill(ir, incomplete_map, lib_name="BO_Designs", cell_name="bad")

    def test_export_from_results_prefers_passing_review_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            bo_netlist = project / "netlist" / "circuit.cir"
            bo_netlist.parent.mkdir()
            bo_netlist.write_text(
                get_topology("5t_ota").generate_circuit(),
                encoding="utf-8",
            )
            candidate_dir = (
                project
                / "agent_review"
                / "candidates"
                / "iter_000_candidate_01"
            )
            candidate_dir.mkdir(parents=True)
            candidate_netlist = candidate_dir / "circuit.cir"
            candidate_netlist.write_text(
                get_topology("two_stage_ota").generate_circuit(),
                encoding="utf-8",
            )
            self._write_results_and_targets(project, bo_netlist)
            self._write_candidate_metrics(
                project / "agent_review" / "candidate_metrics.csv",
                candidate_dir,
                gain=65,
                gbw_mhz=150,
                pm=68,
                power_mw=0.5,
            )

            report = export_from_results(
                project / "results.json",
                lib_name="BO_Designs",
            )

            self.assertEqual(Path(report["netlist_file"]), candidate_netlist)
            self.assertEqual(report["export_source"], "agent_review")
            self.assertEqual(report["target_cell"], "proj_review_opt")

    def test_export_from_results_uses_bo_when_review_candidate_misses_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            bo_netlist = project / "netlist" / "circuit.cir"
            bo_netlist.parent.mkdir()
            bo_netlist.write_text(
                get_topology("5t_ota").generate_circuit(),
                encoding="utf-8",
            )
            candidate_dir = (
                project
                / "agent_review"
                / "candidates"
                / "iter_000_candidate_01"
            )
            candidate_dir.mkdir(parents=True)
            (candidate_dir / "circuit.cir").write_text(
                get_topology("two_stage_ota").generate_circuit(),
                encoding="utf-8",
            )
            self._write_results_and_targets(project, bo_netlist)
            self._write_candidate_metrics(
                project / "agent_review" / "candidate_metrics.csv",
                candidate_dir,
                gain=45,
                gbw_mhz=50,
                pm=68,
                power_mw=0.5,
            )

            report = export_from_results(
                project / "results.json",
                lib_name="BO_Designs",
            )

            self.assertEqual(Path(report["netlist_file"]), bo_netlist)
            self.assertEqual(report["export_source"], "bo_best")
            self.assertEqual(report["target_cell"], "proj_opt")

    def test_prepare_virtuoso_workspace_writes_wrapper_files_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / "import_schematic.il"
            skill_path.write_text("printf(\"loaded\\n\")\n", encoding="utf-8")
            workdir = root / "virtuoso_runs" / "proj"
            user_cds = root / "home" / "cds.lib"
            pdk_path = root / "PDKS" / "TSMC28nm" / "tsmcN28"

            with patch("virtuoso_export.exporter.subprocess.run") as run_mock:
                report = prepare_virtuoso_workspace(
                    skill_path=skill_path,
                    lib_name="BO_Designs",
                    cell_name="proj_opt",
                    tech_lib="tsmcN28",
                    workdir=workdir,
                    run_virtuoso=False,
                    include_cds_libs=[user_cds],
                    pdk_lib_path=pdk_path,
                )

            run_mock.assert_not_called()
            self.assertTrue((workdir / "cds.lib").exists())
            self.assertTrue((workdir / "import_schematic.il").exists())
            self.assertTrue((workdir / "run_import.il").exists())
            self.assertTrue((workdir / "README_import.md").exists())
            cds_lib = (workdir / "cds.lib").read_text(encoding="utf-8")
            self.assertIn(f"SOFTINCLUDE {user_cds}", cds_lib)
            self.assertIn(f"DEFINE tsmcN28 {pdk_path}", cds_lib)
            self.assertIn("DEFINE BO_Designs ./BO_Designs", cds_lib)
            wrapper = (workdir / "run_import.il").read_text(encoding="utf-8")
            self.assertIn('libName = "BO_Designs"', wrapper)
            self.assertIn('cellName = "proj_opt"', wrapper)
            self.assertIn('techLibName = "tsmcN28"', wrapper)
            self.assertIn("libObj = ddCreateLib(libName libPath)", wrapper)
            self.assertIn("techBindTechFile(libObj techLibName)", wrapper)
            self.assertIn("ddReleaseObj(libObj)", wrapper)
            self.assertIn('load(importSkill)', wrapper)
            readme = (workdir / "README_import.md").read_text(encoding="utf-8")
            self.assertIn(f"SOFTINCLUDE `{user_cds}`", readme)
            self.assertIn(f"DEFINE `tsmcN28` `{pdk_path}`", readme)
            self.assertEqual(report["virtuoso_workdir"], str(workdir.resolve()))
            self.assertEqual(report["include_cds_libs"], [str(user_cds)])
            self.assertEqual(report["pdk_lib_path"], str(pdk_path))
            self.assertFalse(report["virtuoso_ran"])

    def test_prepare_virtuoso_workspace_runs_batch_import_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / "import_schematic.il"
            skill_path.write_text("printf(\"loaded\\n\")\n", encoding="utf-8")
            workdir = root / "virtuoso_runs" / "proj"
            cds_log = workdir / "batch_CDS.log"

            with patch("virtuoso_export.exporter.subprocess.run") as run_mock:
                run_mock.return_value.returncode = 0
                run_mock.return_value.stdout = "ok\n"
                report = prepare_virtuoso_workspace(
                    skill_path=skill_path,
                    lib_name="BO_Designs",
                    cell_name="proj_opt",
                    tech_lib="tsmcN28",
                    workdir=workdir,
                    run_virtuoso=True,
                    virtuoso_bin="virtuoso",
                    cds_log_path=cds_log,
                )

            run_mock.assert_called_once()
            command = run_mock.call_args.args[0]
            env = run_mock.call_args.kwargs["env"]
            self.assertEqual(command[:3], ["virtuoso", "-nograph", "-replay"])
            self.assertEqual(Path(command[3]), workdir.resolve() / "run_import.il")
            self.assertEqual(env["CDS_LOG"], str(cds_log.resolve()))
            self.assertTrue(report["virtuoso_ran"])
            self.assertEqual(report["virtuoso_returncode"], 0)
            self.assertEqual(report["cds_log"], str(cds_log.resolve()))
            self.assertEqual(
                (workdir / "virtuoso_import.log").read_text(encoding="utf-8"),
                "ok\n",
            )

    def _write_results_and_targets(self, project: Path, bo_netlist: Path) -> None:
        (project / "results.json").write_text(
            json.dumps(
                {
                    "project_name": "proj",
                    "all_targets_met": True,
                    "netlist_file": str(bo_netlist),
                }
            ),
            encoding="utf-8",
        )
        (project / "optimization_log.json").write_text(
            json.dumps(
                {
                    "targets": {
                        "gain_db": 60,
                        "bandwidth_hz": 100e6,
                        "phase_margin_deg": 60,
                        "power_w": 1e-3,
                    }
                }
            ),
            encoding="utf-8",
        )

    def _write_candidate_metrics(
        self,
        path: Path,
        candidate_dir: Path,
        gain: float,
        gbw_mhz: float,
        pm: float,
        power_mw: float,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "candidate_path",
            "gain_db(dB)",
            "gbw_hz(MHz)",
            "phase_margin_deg(deg)",
            "power_w(mW)",
            "error_message",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(
                {
                    "candidate_path": str(candidate_dir),
                    "gain_db(dB)": str(gain),
                    "gbw_hz(MHz)": str(gbw_mhz),
                    "phase_margin_deg(deg)": str(pm),
                    "power_w(mW)": str(power_mw),
                    "error_message": "",
                }
            )


if __name__ == "__main__":
    unittest.main()
