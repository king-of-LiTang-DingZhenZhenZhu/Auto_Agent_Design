from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdk_cfmom_characterize import (
    MAX_NR,
    MIN_NR,
    parse_capacitances,
    render_netlist,
    select_geometries,
)


class CfmomCharacterizationTests(unittest.TestCase):
    def test_select_geometries_stays_on_documented_grid(self):
        calibration = {
            nr: nr * 0.25e-15 for nr in range(MIN_NR, MAX_NR + 1, 2)
        }

        points = select_geometries(calibration, target_min_f=10e-15)

        self.assertTrue(points)
        for point in points:
            self.assertEqual(point["nr"] % 2, 0)
            self.assertGreaterEqual(point["nr"], MIN_NR)
            self.assertLessEqual(point["nr"], MAX_NR)
            self.assertAlmostEqual(point["lr"] / 10e-9, round(point["lr"] / 10e-9))

    def test_rendered_netlist_uses_confirmed_spectre_parameters(self):
        netlist = render_netlist(
            [
                {
                    "nr": 24,
                    "lr": 1e-6,
                    "w": 50e-9,
                    "s": 50e-9,
                    "stm": 1,
                    "spm": 8,
                    "multi": 1,
                }
            ],
            section="tt",
            temperature_c=27,
        )

        self.assertIn("section=top_tt", netlist)
        self.assertIn("cfmom_2t nr=24 lr=1u w=0.05u s=0.05u stm=1 spm=8", netlist)
        self.assertIn("save Vg0000:p", netlist)

    def test_parse_capacitance_from_psf_ascii_current(self):
        with tempfile.TemporaryDirectory(dir="/share/tmp") as tmp:
            raw = Path(tmp) / "ac.ac"
            raw.write_text(
                'SWEEP\n"freq" "sweep" PROP(\n)\n'
                'TRACE\n"Vg0000:p" "I"\n'
                'VALUE\n"freq" 1.000000000000000e+06\n'
                '"Vg0000:p" (0.00000 -6.283185307e-08)\n'
                '"freq" 1.010000000000000e+06\n',
                encoding="utf-8",
            )

            values = parse_capacitances(raw, 1)

        self.assertAlmostEqual(values[0], 10e-15, places=20)


if __name__ == "__main__":
    unittest.main()
