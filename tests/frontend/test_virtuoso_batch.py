from __future__ import annotations

import os
from unittest.mock import patch
import unittest

from analogskills.eda.virtuoso import make_strmout_command, make_virtuoso_batch_command


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


if __name__ == "__main__":
    unittest.main()
