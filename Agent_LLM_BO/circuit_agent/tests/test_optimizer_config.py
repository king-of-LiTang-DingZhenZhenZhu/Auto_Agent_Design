from __future__ import annotations

import unittest

from config import Settings
from models import DesignTarget
from topologies import get_topology


class OptimizerConfigTest(unittest.TestCase):
    def test_topology_escalation_is_disabled_by_default(self):
        self.assertFalse(Settings().enable_topology_escalation)

    def test_5t_gmid_space_derives_vbias_and_constrains_tail_current(self):
        targets = DesignTarget(
            bandwidth_hz=100e6,
            load_cap_f=1e-12,
        )
        spec = get_topology("5t_ota").get_gmid_spec(targets)
        self.assertIsNotNone(spec)
        self.assertNotIn(
            "VBIAS",
            [param.name for param in spec.pass_through_params],
        )
        self.assertEqual(spec.derived_gate_biases[0].param_name, "VBIAS")

        tail = next(
            branch for branch in spec.branch_currents if branch.name == "I_tail"
        )
        expected_min = 2.0 * 3.141592653589793 * 100e6 * 1e-12 / (24.0 * 0.5)
        self.assertAlmostEqual(tail.low, expected_min)
        self.assertGreaterEqual(tail.default, tail.low)

    def test_all_tail_devices_use_point_two_volt_vds_estimate(self):
        for name in (
            "5t_ota",
            "two_stage_ota",
            "folded_cascode",
            "nmcf_three_stage",
        ):
            spec = get_topology(name).get_gmid_spec()
            tail_devices = [
                transistor
                for transistor in spec.transistors
                if "tail" in transistor.role
            ]
            self.assertTrue(tail_devices)
            for transistor in tail_devices:
                self.assertEqual(transistor.Vds_estimate, 0.2)

    def test_two_stage_current_bounds_follow_gbw_cl_estimate(self):
        targets = DesignTarget(
            bandwidth_hz=100e6,
            load_cap_f=1e-12,
        )
        spec = get_topology("two_stage_ota").get_gmid_spec(targets)
        currents = {branch.name: branch for branch in spec.branch_currents}
        x = 2.0 * 3.141592653589793 * 100e6 * (0.5e-12) / 24.0
        self.assertAlmostEqual(currents["I_tail"].low, 2.0 * x)
        self.assertAlmostEqual(currents["I_cs"].low, 4.0 * x)

    def test_folded_current_bounds_follow_ten_x_budget(self):
        targets = DesignTarget(
            bandwidth_hz=100e6,
            load_cap_f=1e-12,
        )
        spec = get_topology("folded_cascode").get_gmid_spec(targets)
        currents = {branch.name: branch for branch in spec.branch_currents}
        x = 2.0 * 3.141592653589793 * 100e6 * (0.5e-12) / 22.0
        self.assertAlmostEqual(currents["I_tail"].low, 2.0 * x)
        self.assertAlmostEqual(currents["I_fold"].low, 2.0 * x)
        self.assertAlmostEqual(currents["I_cs"].low, 4.0 * x)
        total_min = (
            currents["I_tail"].low
            + 2.0 * currents["I_fold"].low
            + currents["I_cs"].low
        )
        self.assertAlmostEqual(total_min, 10.0 * x)


if __name__ == "__main__":
    unittest.main()
