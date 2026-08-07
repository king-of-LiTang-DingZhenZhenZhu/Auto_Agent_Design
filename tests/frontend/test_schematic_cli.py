from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from run_full_flow import parse_args


class SchematicCliTest(unittest.TestCase):
    def test_new_schematic_actions_are_available(self):
        with patch.object(sys, "argv", ["run_full_flow.py", "--project", "outputs/demo", "--prepare-schematic"]):
            prepared = parse_args()
        with patch.object(sys, "argv", ["run_full_flow.py", "--project", "outputs/demo", "--import-schematic"]):
            imported = parse_args()

        self.assertTrue(prepared.prepare_schematic)
        self.assertTrue(imported.import_schematic)

    def test_removed_export_virtuoso_flag_is_rejected(self):
        with patch.object(sys, "argv", ["run_full_flow.py", "--project", "outputs/demo", "--export-virtuoso"]):
            with self.assertRaises(SystemExit):
                parse_args()

    def test_terminal_implementation_actions_are_mutually_exclusive(self):
        with patch.object(sys, "argv", [
            "run_full_flow.py",
            "--project",
            "outputs/demo",
            "--prepare-schematic",
            "--run-signoff",
        ]):
            with self.assertRaises(SystemExit):
                parse_args()


if __name__ == "__main__":
    unittest.main()
