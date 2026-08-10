from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from topologies import get_topology
from virtuoso_schematic_generation.exporter import (
    export_from_results,
    prepare_virtuoso_workspace,
    select_export_netlist,
)
from virtuoso_schematic_generation.models import DEFAULT_DEVICE_MAP, default_device_map
from virtuoso_schematic_generation.parser import parse_netlist
from virtuoso_schematic_generation.skill_writer import write_skill


class VirtuosoExportTest(unittest.TestCase):
    def test_parse_folded_cascode_instances_and_ports(self):
        netlist = get_topology("folded_cascode_two_stage").generate_circuit()

        ir = parse_netlist(netlist)

        self.assertEqual(ir.subckt_name, "folded_cascode_two_stage")
        self.assertEqual(ir.ports, ["vip", "vin", "vout", "ibias", "vdd", "vss"])

        mos_instances = [inst for inst in ir.instances if inst.kind == "mos"]
        self.assertEqual(len(mos_instances), 36)

        m3_1 = next(inst for inst in ir.instances if inst.name == "M3_1")
        m3_6 = next(inst for inst in ir.instances if inst.name == "M3_6")
        m13_1 = next(inst for inst in ir.instances if inst.name == "M13_1")
        m13_6 = next(inst for inst in ir.instances if inst.name == "M13_6")
        self.assertEqual(m3_1.nodes, ["VB2", "VB2", "net5_1", "vdd"])
        self.assertEqual(m3_6.nodes, ["net5_5", "VB2", "vdd", "vdd"])
        self.assertEqual(m13_1.nodes, ["VB3", "VB3", "net2_1", "vss"])
        self.assertEqual(m13_6.nodes, ["net2_5", "VB3", "vss", "vss"])

        mtailp = next(inst for inst in ir.instances if inst.name == "Mtailp")
        self.assertEqual(mtailp.model, "pch_lvt_mac")
        self.assertEqual(mtailp.nodes, ["ntail", "VB1", "vdd", "vdd"])
        self.assertEqual(mtailp.params["W"], "1.78943u")
        self.assertEqual(mtailp.params["L"], "302.904n")
        self.assertEqual(mtailp.params["nf"], "1")
        self.assertEqual(mtailp.params["m"], "4")

    def test_parse_resistor_and_capacitor(self):
        netlist = get_topology("folded_cascode_two_stage").generate_circuit()

        ir = parse_netlist(netlist)

        rz = next(inst for inst in ir.instances if inst.name == "Rz")
        cc = next(inst for inst in ir.instances if inst.name == "Cc")
        self.assertEqual(rz.kind, "res")
        self.assertEqual(rz.nodes, ["nstage1", "n_rz"])
        self.assertEqual(rz.params["R"], "3.83773k")
        self.assertEqual(cc.kind, "cap")
        self.assertEqual(cc.nodes, ["n_rz", "vout"])
        self.assertEqual(cc.params["C"], "255.856f")

    def test_parse_and_export_mos_instances_with_non_m_prefix(self):
        ir = parse_netlist(get_topology("strongarm_latch").generate_circuit())

        s1 = next(inst for inst in ir.instances if inst.name == "S1")
        self.assertEqual(s1.kind, "mos")
        self.assertEqual(len(s1.nodes), 4)

        skill = write_skill(
            ir,
            DEFAULT_DEVICE_MAP,
            lib_name="BO_Designs",
            cell_name="strongarm_latch_opt",
        )
        self.assertIn('"S1"', skill)
        self.assertIn('inst_S1 = dbCreateParamInst', skill)

    def test_skill_writer_contains_target_and_instances(self):
        ir = parse_netlist(get_topology("5t_ota").generate_circuit())

        skill = write_skill(
            ir,
            DEFAULT_DEVICE_MAP,
            lib_name="BO_Designs",
            cell_name="ota_5t_opt",
        )

        self.assertIn('ddGetObj("BO_Designs")', skill)
        self.assertIn(
            'dbOpenCellViewByType("BO_Designs" "ota_5t_opt" "schematic"',
            skill,
        )
        self.assertIn('dbCreateParamInst(cv dbOpenCellViewByType', skill)
        self.assertIn('"Mtail" 0:6 "R0"', skill)
        self.assertIn('"Mdp1" -3:3 "R0"', skill)
        self.assertIn("dbFindTermByName", skill)
        self.assertIn("dbTransformPoint", skill)
        self.assertIn("schCreateWireLabel", skill)
        self.assertNotIn("dbCreateConnByName", skill)
        expected_pin_cells = {
            "vip": "ipin",
            "vin": "ipin",
            "vout": "opin",
            "vbias": "ipin",
            "vdd": "iopin",
            "vss": "iopin",
        }
        for port, pin_cell in expected_pin_cells.items():
            self.assertIn(
                f'dbOpenCellViewByType("basic" "{pin_cell}" "symbol"',
                skill,
            )
            self.assertIn(f') "{port}" ', skill)

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

        self.assertIn('list("m" "int" 4)', skill)
        self.assertIn('list("nf" "int" 5)', skill)
        self.assertIn('"Mtail" -7.5:3 "R0"', skill)
        self.assertIn('schCreateWireLabel(cv car(wireObjs) stubXY "vout"', skill)

    def test_skill_writer_uses_pcell_parameters_for_mapped_passives(self):
        ir = parse_netlist(
            """
simulator lang=spectre
subckt mapped_passives in out vss
R1 (in out) rupolym w=1u l=10u m=2
C1 (out vss) cfmom_2t nr=24 lr=1u w=0.05u s=0.05u stm=1 spm=8
ends mapped_passives
"""
        )

        skill = write_skill(
            ir,
            default_device_map(),
            lib_name="BO_Designs",
            cell_name="mapped_passives_opt",
        )

        self.assertIn('dbOpenCellViewByType("tsmcN28" "rupolym" "symbol"', skill)
        self.assertIn('dbOpenCellViewByType("tsmcN28" "cfmom_2t" "symbol"', skill)
        self.assertIn('list("m" "int" 2)', skill)
        self.assertIn('list("Nfinger" "int" 24)', skill)
        self.assertIn('list("Wfinger" "string" "50n")', skill)

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

    def test_structured_custom_goals_fail_closed_for_review_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "strongarm"
            project.mkdir(parents=True)
            bo_netlist = project / "circuit.cir"
            bo_netlist.write_text(
                get_topology("strongarm_latch").generate_circuit(),
                encoding="utf-8",
            )
            candidate_dir = project / "agent_review" / "candidate"
            candidate_dir.mkdir(parents=True)
            candidate_netlist = candidate_dir / "circuit.cir"
            candidate_netlist.write_text(
                bo_netlist.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self._write_candidate_metrics(
                project / "agent_review" / "candidate_metrics.csv",
                candidate_dir,
                gain=0,
                gbw_mhz=0,
                pm=0,
                power_mw=0.01,
            )
            (project / "optimization_log.json").write_text(
                json.dumps({
                    "targets": {
                        "power_w": 100e-6,
                        "metric_goals": {
                            "power_w": {
                                "constraint": "max",
                                "target": 100e-6,
                            },
                            "decision_positive_margin_v": {
                                "constraint": "min",
                                "target": 0.45,
                            },
                        },
                    }
                }),
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps({
                    "all_targets_met": True,
                    "netlist_file": str(bo_netlist),
                }),
                encoding="utf-8",
            )

            selected, source = select_export_netlist(results)

            self.assertEqual(selected, bo_netlist)
            self.assertEqual(source, "bo_best")

    def test_unverified_passive_realization_blocks_export_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(
                get_topology("two_stage_ota").generate_circuit(),
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps({"all_targets_met": True, "netlist_file": str(netlist)}),
                encoding="utf-8",
            )
            passive_dir = project / "passive_realization"
            passive_dir.mkdir()
            (passive_dir / "passive_realization.json").write_text(
                json.dumps({"required": True, "verified": False}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not passed nominal"):
                select_export_netlist(results)

    def test_required_passive_realization_missing_report_blocks_export_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(
                get_topology("two_stage_ota").generate_circuit(),
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps({
                    "all_targets_met": True,
                    "netlist_file": str(netlist),
                    "passive_realization_required": True,
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "requires PDK passive"):
                select_export_netlist(results)

    def test_verified_passive_realization_is_export_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            bo_netlist = project / "bo.cir"
            bo_netlist.write_text(
                get_topology("two_stage_ota").generate_circuit(),
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps({"all_targets_met": True, "netlist_file": str(bo_netlist)}),
                encoding="utf-8",
            )
            passive_dir = project / "passive_realization"
            passive_dir.mkdir()
            realized = passive_dir / "circuit.cir"
            realized.write_text(bo_netlist.read_text(encoding="utf-8"), encoding="utf-8")
            (passive_dir / "passive_realization.json").write_text(
                json.dumps({
                    "required": True,
                    "verified": True,
                    "netlist_file": str(realized),
                }),
                encoding="utf-8",
            )

            selected, source = select_export_netlist(results)

            self.assertEqual(selected, realized)
            self.assertEqual(source, "passive_realization")

    def test_prepare_virtuoso_workspace_writes_wrapper_files_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / "import_schematic.il"
            skill_path.write_text("printf(\"loaded\\n\")\n", encoding="utf-8")
            workdir = root / "virtuoso_runs" / "proj"
            user_cds = root / "home" / "cds.lib"
            pdk_path = root / "PDKS" / "TSMC28nm" / "tsmcN28"

            with patch("virtuoso_schematic_generation.exporter.subprocess.run") as run_mock:
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
            self.assertIn(
                "DEFINE basic $CDSHOME/tools/dfII/etc/cdslib/basic",
                cds_lib,
            )
            self.assertIn(
                "DEFINE analogLib $CDSHOME/tools/dfII/etc/cdslib/artist/analogLib",
                cds_lib,
            )
            wrapper = (workdir / "run_import.il").read_text(encoding="utf-8")
            self.assertIn('libName = "BO_Designs"', wrapper)
            self.assertIn('cellName = "proj_opt"', wrapper)
            self.assertIn('techLibName = "tsmcN28"', wrapper)
            self.assertIn("libObj = ddCreateLib(libName libPath)", wrapper)
            self.assertIn("techBindTechFile(libObj techLibName)", wrapper)
            self.assertIn("ddReleaseObj(libObj)", wrapper)
            self.assertIn('load(importSkill)', wrapper)
            self.assertIn("regExitBefore('AStidyUpAtExit)", wrapper)
            self.assertIn("exit()", wrapper)
            self.assertNotIn("exit(0)", wrapper)
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

            with patch("virtuoso_schematic_generation.exporter.subprocess.run") as run_mock:
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
