from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from operating_point import evaluate_dc_operating_points


class OperatingPointEvaluatorTest(unittest.TestCase):
    def _write_csv(self, root: Path, rows: str) -> Path:
        path = root / "dc_operating_points.csv"
        path.write_text(
            "\n".join(
                [
                    "instance,model,vd,vg,vs,id,ids,gm,gds,vgs,vds,vth,vdsat,gmoverid",
                    rows,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_saturated_device_has_no_penalty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(
                Path(tmp),
                "Mcs,pch,0,0,0,0,0,1e-3,1e-5,0.6,0.35,0.4,0.20,10",
            )
            status = evaluate_dc_operating_points(path, {"Mcs"})

            self.assertEqual(status.critical_linear_count, 0)
            self.assertEqual(status.critical_near_edge_count, 0)
            self.assertAlmostEqual(status.penalty, 0.0)
            self.assertTrue(status.passed)

    def test_critical_near_edge_gets_soft_penalty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(
                Path(tmp),
                "Mcs,pch,0,0,0,0,0,1e-3,1e-5,0.6,0.22,0.4,0.20,10",
            )
            status = evaluate_dc_operating_points(path, {"Mcs"})

            self.assertEqual(status.critical_linear_count, 0)
            self.assertEqual(status.critical_near_edge_count, 1)
            self.assertLess(status.penalty, 0.0)
            self.assertTrue(status.passed)

    def test_critical_linear_gets_strong_penalty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(
                Path(tmp),
                "Mcs,pch,0,0,0,0,0,1e-3,1e-5,0.6,0.12,0.4,0.20,10",
            )
            status = evaluate_dc_operating_points(path, {"Mcs"})

            self.assertEqual(status.critical_linear, ["Mcs"])
            self.assertFalse(status.passed)
            self.assertLessEqual(status.penalty, -80.0)

    def test_noncritical_linear_is_reported_without_penalty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(
                Path(tmp),
                "Mbias,pch,0,0,0,0,0,1e-3,1e-5,0.6,0.12,0.4,0.20,10",
            )
            status = evaluate_dc_operating_points(path, {"Mcs"})

            self.assertEqual(status.critical_linear_count, 0)
            self.assertEqual(status.noncritical_linear, ["Mbias"])
            self.assertEqual(status.penalty, 0.0)
            self.assertTrue(status.passed)


if __name__ == "__main__":
    unittest.main()
