from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from netlist_utils import load_relative_ahdl_includes, split_monolithic_netlist


class NetlistUtilsTest(unittest.TestCase):
    def test_splits_subcircuit_from_testbench(self):
        content = """.include model.scs
.subckt ota in out vdd vss
M1 out in vss vss nch
.ends ota
VDD vdd 0 0.9
X1 in out vdd 0 ota
.end
"""

        circuit, testbench = split_monolithic_netlist(content)

        self.assertIn(".subckt ota", circuit)
        self.assertIn(".ends ota", circuit)
        self.assertNotIn("X1 in out", circuit)
        self.assertIn("VDD vdd 0 0.9", testbench)
        self.assertIn("X1 in out", testbench)

    def test_wraps_flat_device_netlist(self):
        circuit, testbench = split_monolithic_netlist(
            "M1 vout vin 0 0 nch\n.op\n.end\n"
        )

        self.assertIn(".subckt dut", circuit)
        self.assertIn("M1 vout vin 0 0 nch", circuit)
        self.assertEqual(".op\n.end", testbench)

    def test_loads_relative_ahdl_include(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.va").write_text("module model; endmodule\n", encoding="utf-8")
            loaded = load_relative_ahdl_includes([
                ('ahdl_include "model.va"', root)
            ])
        self.assertEqual(loaded, {"model.va": "module model; endmodule\n"})

    def test_rejects_unsafe_ahdl_include(self):
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            load_relative_ahdl_includes([
                ('ahdl_include "../model.va"', Path("/tmp"))
            ])


if __name__ == "__main__":
    unittest.main()
