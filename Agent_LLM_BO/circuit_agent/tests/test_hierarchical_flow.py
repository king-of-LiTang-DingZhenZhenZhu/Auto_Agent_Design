from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess

from hierarchical_flow import HierarchicalFlow, HierarchicalFlowError
from models import DesignTarget
from pdk_profiles import get_pdk_profile
from topologies import get_topology


class FakeFlowEnvironment:
    def __init__(
        self,
        output_root: Path,
        *,
        nominal_pass: bool = True,
        pvt_pass: bool = True,
        pdk_mismatch: bool = False,
        bad_interface: bool = False,
    ) -> None:
        self.output_root = output_root
        self.nominal_pass = nominal_pass
        self.pvt_pass = pvt_pass
        self.pdk_mismatch = pdk_mismatch
        self.bad_interface = bad_interface
        self.commands: list[list[str]] = []

    def run_command(self, command, **_kwargs):
        self.commands.append(command)
        project_name = command[command.index("--project") + 1]
        netlist_path = Path(command[command.index("--netlist") + 1])
        output = self.output_root / project_name
        netlist = output / "netlist" / "circuit.cir"
        netlist.parent.mkdir(parents=True, exist_ok=True)
        text = netlist_path.read_text(encoding="utf-8")
        if self.bad_interface and "folded_cascode_two_stage" in netlist_path.name:
            text = "subckt wrong_opamp (vip vin vout ibias vdd vss)\nends wrong_opamp\n"
        netlist.write_text(text, encoding="utf-8")
        (output / "results.json").write_text(
            json.dumps(
                {
                    "all_targets_met": self.nominal_pass,
                    "netlist_file": str(netlist),
                }
            ),
            encoding="utf-8",
        )
        pdk = get_pdk_profile().to_dict()
        if self.pdk_mismatch and "folded_cascode_two_stage" in netlist_path.name:
            pdk["name"] = "unexpected_pdk"
        (output / "pdk_profile_used.json").write_text(
            json.dumps(pdk), encoding="utf-8"
        )
        return CompletedProcess(command, 0, stdout="ok", stderr="")

    def run_pvt(self, *, results_path, **_kwargs):
        project = Path(results_path).parent
        pvt_root = project / "pvt"
        pvt_root.mkdir(parents=True, exist_ok=True)
        report = {"pvt_pass": self.pvt_pass, "pvt_root": str(pvt_root)}
        (pvt_root / "pvt_results.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return report


class HierarchicalFlowTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "bandgap"
        get_topology("bandgap_ptat").write_project(
            project,
            targets=DesignTarget(power_w=1e-3, load_cap_f=1e-12),
            original_requirement="hierarchical bandgap",
        )
        return project

    def _flow(self, project: Path, environment: FakeFlowEnvironment, **kwargs):
        return HierarchicalFlow(
            project,
            output_root=environment.output_root,
            command_runner=environment.run_command,
            pvt_runner=environment.run_pvt,
            **kwargs,
        )

    def test_success_freezes_child_and_runs_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = FakeFlowEnvironment(root / "outputs")
            project = self._project(root)

            state = self._flow(project, environment).run()

            artifact = project / "child_blocks" / "opamp" / "artifact"
            self.assertEqual(state["children"], {"opamp": "optimized"})
            self.assertTrue((artifact / "circuit.cir").exists())
            self.assertTrue((artifact / "pvt" / "pvt_results.json").exists())
            manifest = json.loads((artifact / "artifact.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["nominal_pass"])
            self.assertTrue(manifest["pvt_pass"])
            self.assertEqual(len(environment.commands), 2)
            parent_netlist = (project / "bandgap_ptat.cir").read_text(encoding="utf-8")
            self.assertIn("subckt folded_cascode_two_stage", parent_netlist)

    def test_child_nominal_failure_stops_before_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = FakeFlowEnvironment(root / "outputs", nominal_pass=False)

            with self.assertRaisesRegex(HierarchicalFlowError, "nominal"):
                self._flow(self._project(root), environment).run()

            self.assertEqual(len(environment.commands), 1)

    def test_child_pvt_failure_stops_before_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = FakeFlowEnvironment(root / "outputs", pvt_pass=False)

            with self.assertRaisesRegex(HierarchicalFlowError, "PVT"):
                self._flow(self._project(root), environment).run()

            self.assertEqual(len(environment.commands), 1)

    def test_pdk_and_interface_mismatch_stop_before_parent(self):
        for mismatch in ("pdk_mismatch", "bad_interface"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                environment = FakeFlowEnvironment(
                    root / "outputs", **{mismatch: True}
                )

                with self.assertRaises(HierarchicalFlowError):
                    self._flow(self._project(root), environment).run()

                self.assertEqual(len(environment.commands), 1)

    def test_qualified_artifact_is_reused_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = FakeFlowEnvironment(root / "outputs")
            project = self._project(root)

            self._flow(project, environment).run()
            first_count = len(environment.commands)
            reused = self._flow(project, environment).run()
            reused_count = len(environment.commands)
            self._flow(project, environment, force_child=True).run()

            self.assertEqual(reused["children"], {"opamp": "reused"})
            self.assertEqual(reused_count - first_count, 1)
            self.assertEqual(len(environment.commands) - reused_count, 2)


if __name__ == "__main__":
    unittest.main()
