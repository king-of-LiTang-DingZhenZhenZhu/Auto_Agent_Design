from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings
from simulator import Simulator


class SimulatorPathTests(unittest.TestCase):
    def test_run_spectre_uses_absolute_paths_with_run_dir_as_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                run_dir = Path("outputs/project/pvt/corners/tt")
                run_dir.mkdir(parents=True)
                testbench = run_dir / "tb.scs"
                testbench.write_text("simulator lang=spectre\n", encoding="utf-8")

                simulator = Simulator(Settings(dry_run=False))
                with patch("simulator.subprocess.run") as run_mock:
                    run_mock.return_value = SimpleNamespace(
                        returncode=0,
                        stdout="",
                        stderr="",
                    )
                    success, _, error = simulator.run_spectre(testbench, run_dir)

                self.assertTrue(success)
                self.assertEqual(error, "")
                command = run_mock.call_args.args[0]
                kwargs = run_mock.call_args.kwargs
                absolute_run_dir = run_dir.resolve()
                self.assertEqual(kwargs["cwd"], str(absolute_run_dir))
                self.assertIn(str(testbench.resolve()), command)
                self.assertIn(str(absolute_run_dir / "raw"), command)
                self.assertIn(str(absolute_run_dir / "sim.log"), command)
                self.assertNotIn(
                    str(absolute_run_dir / testbench),
                    command,
                )
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
