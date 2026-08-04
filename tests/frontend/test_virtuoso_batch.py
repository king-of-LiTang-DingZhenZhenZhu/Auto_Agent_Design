from __future__ import annotations

import os
from unittest.mock import patch
import unittest

from analogskills.eda.virtuoso import make_virtuoso_batch_command


class VirtuosoBatchCommandTest(unittest.TestCase):
    def test_nograph_is_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            command = make_virtuoso_batch_command("run.il").command
        self.assertEqual(command, ("virtuoso", "-nograph", "-replay", "run.il"))

    def test_nograph_can_be_disabled_for_existing_display(self):
        with patch.dict(os.environ, {"ANALOGSKILLS_VIRTUOSO_NOGRAPH": "false"}, clear=True):
            command = make_virtuoso_batch_command("run.il").command
        self.assertEqual(command, ("virtuoso", "-replay", "run.il"))


if __name__ == "__main__":
    unittest.main()
