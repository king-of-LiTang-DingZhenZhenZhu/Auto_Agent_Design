from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from analogskills.eda.virtuoso import (
    make_strmout_command,
    make_virtuoso_batch_command,
    write_virtuoso_session_skill,
)


class VirtuosoBatchCommandTest(unittest.TestCase):
    def test_nograph_is_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            command = make_virtuoso_batch_command("run.il").command
        self.assertEqual(command, ("virtuoso", "-nograph", "-replay", "run.il"))

    def test_nograph_can_be_disabled_for_existing_display(self):
        with patch.dict(os.environ, {"ANALOGSKILLS_VIRTUOSO_NOGRAPH": "false"}, clear=True):
            command = make_virtuoso_batch_command("run.il").command
        self.assertEqual(command, ("virtuoso", "-replay", "run.il"))

    def test_strmout_uses_noninteractive_xstream_binary(self):
        command = make_strmout_command(
            lib="BO_Designs",
            cell="ota",
            output_path="ota.gds",
            run_dir="physical",
            log_file="streamout.log",
        ).command
        self.assertEqual(command[:7], ("strmout", "-library", "BO_Designs", "-strmFile", "ota.gds", "-topCell", "ota"))
        self.assertIn("-runDir", command)
        self.assertNotIn("-replay", command)

    def test_session_script_loads_components_and_cancels_display_drf_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "schematic.il"
            second = root / "layout.il"
            first.write_text("schematicDone = t\n", encoding="utf-8")
            second.write_text("layoutDone = t\n", encoding="utf-8")

            session = write_virtuoso_session_skill(root / "write_all.il", (first, second))
            text = session.read_text(encoding="utf-8")

            self.assertEqual(text.count("load("), 2)
            self.assertIn("regExitBefore('AStidyUpAtExit)", text)
            self.assertIn("hiFormCancel(techSaveDrmForm)", text)
            self.assertTrue(text.rstrip().endswith("exit()"))


if __name__ == "__main__":
    unittest.main()
