from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from analogskills.imported_design.flow import _run_oa_write_stage, _run_virtuoso_skill_stage


class VirtuosoExecutionTest(unittest.TestCase):
    def test_unchanged_oa_fingerprint_skips_second_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oa = root / "oa"
            oa.mkdir()
            schematic = oa / "schematic.il"
            layout = oa / "layout.il"
            batch = oa / "write_all.il"
            schematic.write_text("schematicDone = t\n", encoding="utf-8")
            layout.write_text("layoutDone = t\n", encoding="utf-8")
            batch.write_text("exit()\n", encoding="utf-8")
            manifest = {
                "cellview": {"lib": "BO_Designs", "cell": "ota"},
                "artifacts": {
                    "schematic_skill": str(schematic),
                    "layout_skill": str(layout),
                    "oa_batch_skill": str(batch),
                },
            }
            completed = {"name": "oa_write", "ok": True, "executor": "batch"}

            with patch("analogskills.imported_design.flow._run_virtuoso_skill_stage", return_value=completed) as run:
                first = _run_oa_write_stage(root, manifest, {"virtuoso": "virtuoso"})
                second = _run_oa_write_stage(root, manifest, {"virtuoso": "virtuoso"})

            self.assertTrue(first["ok"])
            self.assertTrue(second["skipped"])
            self.assertEqual(run.call_count, 1)
            state = json.loads((oa / "oa_stage_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "completed")

    def test_running_skill_server_is_reused_without_batch_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            port = root / "skill_server_port.txt"
            port.write_text("5555", encoding="utf-8")
            live = root / "layout.il"
            batch = root / "layout_batch.il"
            live.write_text("layoutDone = t\n", encoding="utf-8")
            batch.write_text("exit()\n", encoding="utf-8")
            client = MagicMock()
            client.ping.return_value = MagicMock(ok=True, error="")

            with patch.dict(os.environ, {
                "ANALOGSKILLS_VIRTUOSO_EXECUTION": "auto",
                "ANALOGSKILLS_SKILL_SERVER_PORT_FILE": str(port),
            }, clear=False), patch(
                "analogskills.imported_design.flow.VirtuosoSkillClient",
                return_value=client,
            ), patch(
                "analogskills.imported_design.flow.run_skill_file",
                return_value="t",
            ) as run_skill, patch(
                "analogskills.imported_design.flow.run_eda_command",
            ) as run_batch:
                record = _run_virtuoso_skill_stage(
                    root,
                    "layout_oa",
                    live,
                    batch,
                    {"virtuoso": "virtuoso"},
                )

            self.assertTrue(record["ok"])
            self.assertEqual(record["executor"], "skill_server")
            run_skill.assert_called_once_with(client, live)
            run_batch.assert_not_called()
            client.disconnect.assert_called_once_with()
            client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
