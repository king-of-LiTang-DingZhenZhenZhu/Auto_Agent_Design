from __future__ import annotations

import unittest

from models import DerivedGateBiasSpec


class GmidBiasDerivationTest(unittest.TestCase):
    def _resolve_bias(self, device_type: str, source_voltage: float) -> float:
        bias = DerivedGateBiasSpec(
            role=f"{device_type}_bias",
            param_name="VBIAS",
            supply_voltage=source_voltage,
            device_type=device_type,
        )
        return bias.resolve_gate_voltage(0.42)

    def test_nmos_gate_bias_adds_vgs_to_source_voltage(self):
        self.assertAlmostEqual(self._resolve_bias("nmos", 0.0), 0.42)

    def test_pmos_gate_bias_subtracts_vsg_from_source_voltage(self):
        self.assertAlmostEqual(self._resolve_bias("pmos", 0.9), 0.48)


if __name__ == "__main__":
    unittest.main()
