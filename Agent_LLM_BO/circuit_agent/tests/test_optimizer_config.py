from __future__ import annotations

import unittest
from unittest.mock import patch

from config import Settings
from models import DesignTarget
from topologies import get_topology


class OptimizerConfigTest(unittest.TestCase):
    def test_topology_escalation_is_disabled_by_default(self):
        self.assertFalse(Settings().enable_topology_escalation)

    def test_default_gmid_lookup_table_path_exists(self):
        from pathlib import Path

        self.assertTrue(Path(Settings().gmid_table_path).exists())

    def test_gmid_lookup_table_path_can_come_from_env(self):
        with patch.dict("os.environ", {"GMID_TABLE_PATH": "/tmp/custom_gmid.json"}):
            self.assertEqual(Settings().gmid_table_path, "/tmp/custom_gmid.json")

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
        x = 2.0 * 3.141592653589793 * 100e6 * (0.5e-12) / 15.0
        self.assertAlmostEqual(currents["I_tail"].low, max(50e-6, 2.0 * x))
        self.assertAlmostEqual(currents["I_tail"].high, 10.0 * currents["I_tail"].low)
        self.assertNotIn("I_cs", currents)

    def test_two_stage_gmid_space_uses_integer_mirror_ratio(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        param_space = spec.build_param_space()
        params = {param.name: param for param in param_space.params}
        self.assertIn("I_tail", params)
        self.assertIn("ratio_load_tail", params)
        self.assertNotIn("I_cs", params)
        self.assertNotIn("gm_id_load_nmos", params)
        self.assertNotIn("L_load_nmos", params)
        self.assertNotIn("L_cs_pmos", params)
        ratio = params["ratio_load_tail"]
        self.assertEqual(ratio.value_type, "int")
        self.assertEqual(ratio.low, 1)
        self.assertEqual(ratio.high, 3)

    def test_current_source_and_load_lengths_are_constrained(self):
        physical_roles = {
            "5t_ota": ("Ltail", "Lcm"),
            "two_stage_ota": ("Ltail", "Lload"),
            "folded_cascode": ("Ltailp", "Lfoldn", "Lload"),
            "nmcf_three_stage": ("Ltail1", "Lload1", "Lload2", "Lload3"),
        }
        gmid_roles = {
            "5t_ota": ("tail_pmos", "mirror_nmos"),
            "two_stage_ota": ("tail_nmos", "load_nmos"),
            "folded_cascode": ("tail_pmos", "fold_nmos", "load_nmos"),
            "nmcf_three_stage": (
                "stage1_tail_pmos",
                "stage1_load_nmos",
                "stage2_load_pmos",
                "stage3_load_nmos",
            ),
        }

        for topology_name, param_names in physical_roles.items():
            params = {
                param.name: param
                for param in get_topology(topology_name).get_param_space().params
            }
            for param_name in param_names:
                self.assertAlmostEqual(params[param_name].low, 200e-9)
                self.assertAlmostEqual(params[param_name].high, 600e-9)

        for topology_name, roles in gmid_roles.items():
            transistors = {
                transistor.role: transistor
                for transistor in get_topology(topology_name).get_gmid_spec().transistors
            }
            for role in roles:
                self.assertAlmostEqual(transistors[role].L_low, 200e-9)
                self.assertAlmostEqual(transistors[role].L_high, 600e-9)

    def test_second_stage_cs_length_is_bound_to_load_length(self):
        for topology_name in ("two_stage_ota", "folded_cascode"):
            topology = get_topology(topology_name)
            param_names = [param.name for param in topology.get_param_space().params]
            circuit = topology.generate_circuit()
            spec = topology.get_gmid_spec()
            gmid_params = {param.name for param in spec.build_param_space().params}

            self.assertNotIn("Lcs", param_names)
            self.assertNotIn("Lcs=", circuit)
            self.assertIn("Mcs", circuit)
            self.assertRegex(circuit, r"Mcs .* l=Lload\b")
            self.assertNotIn("L_cs_pmos", gmid_params)

    def test_two_stage_gmid_space_derives_nmos_vbias(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        self.assertNotIn(
            "VBIAS",
            [param.name for param in spec.pass_through_params],
        )
        self.assertEqual(len(spec.derived_gate_biases), 1)
        bias = spec.derived_gate_biases[0]
        self.assertEqual(bias.role, "tail_nmos")
        self.assertEqual(bias.param_name, "VBIAS")
        self.assertEqual(bias.device_type, "nmos")
        self.assertEqual(bias.supply_voltage, 0.0)

    def test_two_stage_diff_pair_uses_negative_body_bias_for_lookup(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        transistors = {transistor.role: transistor for transistor in spec.transistors}

        self.assertEqual(transistors["diff_pair_nmos"].Vbs, -0.3)
        self.assertEqual(transistors["tail_nmos"].Vbs, 0.0)
        self.assertEqual(transistors["load_nmos"].Vbs, 0.0)

    def test_gmid_specs_use_lookup_supported_body_biases(self):
        supported_vbs = {-0.3, 0.0}
        for name in (
            "5t_ota",
            "two_stage_ota",
            "folded_cascode",
            "nmcf_three_stage",
        ):
            spec = get_topology(name).get_gmid_spec()
            for transistor in spec.transistors:
                self.assertIn(
                    transistor.Vbs,
                    supported_vbs,
                    f"{name}:{transistor.role} Vbs={transistor.Vbs}",
                )

    def test_vbias_physical_ranges_are_topology_owned(self):
        five_t_params = {
            param.name: param for param in get_topology("5t_ota").get_param_space().params
        }
        two_stage_params = {
            param.name: param
            for param in get_topology("two_stage_ota").get_param_space().params
        }

        self.assertEqual(five_t_params["VBIAS"].low, 0.15)
        self.assertEqual(five_t_params["VBIAS"].high, 0.55)
        self.assertEqual(two_stage_params["VBIAS"].low, 0.4)
        self.assertEqual(two_stage_params["VBIAS"].high, 0.85)

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
