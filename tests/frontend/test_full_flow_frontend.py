from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from config import Settings
from full_flow_frontend import (
    ensure_physical_topology_supported,
    load_pvt_targets,
    optimizer_command,
    prepare_frontend_project,
    run_automatic_review,
)


class FullFlowFrontendTest(unittest.TestCase):
    def test_embedded_pvt_targets_are_separate_from_nominal_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            requirements = Path(tmp) / "requirements.json"
            requirements.write_text(
                json.dumps({
                    "targets": {"gain_db": 45, "phase_margin_deg": 60},
                    "pvt_targets": {"gain_db": 20, "phase_margin_deg": 30},
                }),
                encoding="utf-8",
            )

            targets = load_pvt_targets(requirements_file=requirements)

            self.assertIsNotNone(targets)
            self.assertEqual(targets.gain_db, 20)
            self.assertEqual(targets.phase_margin_deg, 30)

    def test_explicit_pvt_requirements_override_embedded_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.json"
            requirements.write_text(json.dumps({"pvt_targets": {"gain_db": 20}}), encoding="utf-8")
            explicit = root / "pvt.json"
            explicit.write_text(json.dumps({"targets": {"gain_db": 25}}), encoding="utf-8")

            targets = load_pvt_targets(
                requirements_file=requirements,
                pvt_requirements_file=explicit,
            )

            self.assertIsNotNone(targets)
            self.assertEqual(targets.gain_db, 25)

    def test_structured_requirements_use_native_project_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.json"
            requirements.write_text(
                json.dumps(
                    {
                        "project_name": "ota_from_request",
                        "original_requirement": "two-stage OTA",
                        "topology_name": "two_stage_ota",
                        "targets": {
                            "gain_db": 60,
                            "bandwidth_hz": 100e6,
                            "phase_margin_deg": 60,
                            "power_w": 1e-3,
                            "load_cap_f": 1e-12,
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = Settings(outputs_dir=str(root / "outputs"))

            project = prepare_frontend_project(
                requirements_file=requirements,
                input_root=root / "inputs",
                config=config,
            )

            self.assertEqual(project.topology, "two_stage_ota")
            self.assertTrue(project.netlist.is_file())
            self.assertTrue(project.requirements.is_file())
            self.assertGreaterEqual(len(project.testbenches), 3)
            command = optimizer_command(project, max_iterations=7, dry_run=True)
            self.assertIn(str(project.netlist), command)
            self.assertIn("--requirements", command)
            self.assertEqual(command[-3:], ["--max-iter", "7", "--dry-run"])

    def test_free_text_requires_the_existing_frontend_llm_configuration(self):
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            prepare_frontend_project(request="design an OTA", config=Settings(deepseek_api_key=""))

    def test_strongarm_uses_the_native_eleven_mos_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.json"
            requirements.write_text(
                json.dumps(
                    {
                        "project_name": "strongarm_from_request",
                        "topology_name": "strongarm_latch",
                        "topology_hint": "StrongARM latch comparator",
                        "targets": {"power_w": 100e-6},
                    }
                ),
                encoding="utf-8",
            )
            project = prepare_frontend_project(
                requirements_file=requirements,
                input_root=root / "inputs",
                config=Settings(outputs_dir=str(root / "outputs")),
            )

            netlist = project.netlist.read_text(encoding="utf-8")
            self.assertEqual(project.topology, "strongarm_latch")
            for name in ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "S1", "S2", "S3", "S4"):
                self.assertIn(f"{name} ", netlist)

    def test_physical_adapter_scope_is_fail_closed(self):
        ensure_physical_topology_supported("two_stage_ota")
        ensure_physical_topology_supported("strongarm_latch")
        with self.assertRaisesRegex(ValueError, "physical_adapter_required"):
            ensure_physical_topology_supported("5t_ota")

    def test_review_invokes_existing_auto_frontend_command(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0)

        run_automatic_review(
            project_dir="outputs/ota",
            topology="two_stage_ota",
            workspace="workspace",
            dry_run=True,
            runner=runner,
        )

        command, kwargs = calls[0]
        self.assertTrue(command[1].endswith("review_optimization.py"))
        self.assertIn("--simulate", command)
        self.assertIn("--dry-run", command)
        self.assertEqual(kwargs["check"], False)


if __name__ == "__main__":
    unittest.main()
