from __future__ import annotations

import unittest

from pdk_integration.profiles import get_pdk_profile
from topologies import get_topology


class NMCFThreeStageTest(unittest.TestCase):
    def test_structure_matches_leung_nmcf_figure_one_h(self):
        topology = get_topology("nmcf_three_stage")
        circuit = topology.generate_circuit()
        params = set(topology.get_param_space().get_param_names())
        pdk = get_pdk_profile()

        self.assertIn("Mdiff1a (s1_mirr vin tail vdd)", circuit)
        self.assertIn("Mdiff1b (s1_out vip tail vdd)", circuit)
        self.assertIn(
            "Mgm2 (s2_mirr s1_out vdd vdd) " + pdk.pmos_lvt_model,
            circuit,
        )
        self.assertIn(
            "Mmirror2a (s2_mirr s2_mirr vss vss) " + pdk.nmos_lvt_model,
            circuit,
        )
        self.assertIn("Mmirror2b (s2_out s2_mirr vss vss)", circuit)
        self.assertIn("Msource2 (s2_out vbiasp vdd vdd)", circuit)
        self.assertIn("Mgm3 (vout s2_out vss vss)", circuit)
        self.assertIn("Mgmf2 (vout s1_out vdd vdd)", circuit)
        self.assertIn("Cc1 (s1_out vout) capacitor c=Cc1", circuit)
        self.assertIn("Cc2 (s2_out vout) capacitor c=Cc2", circuit)

        self.assertNotIn("Rz1", circuit)
        self.assertNotIn("Rz1", params)
        self.assertNotIn("Wload2", params)
        self.assertNotIn("Wload3", params)
        self.assertTrue(
            {"Wmirror2", "Wsource2", "Wgm3", "Wgmf2"}.issubset(params)
        )
        self.assertEqual(
            topology.required_model_roles(),
            ("nmos_lvt", "pmos_lvt"),
        )

    def test_gmid_contract_models_serial_and_feedforward_paths(self):
        spec = get_topology("nmcf_three_stage").get_gmid_spec()
        transistors = {transistor.role: transistor for transistor in spec.transistors}
        pass_through = {param.name for param in spec.pass_through_params}

        self.assertEqual(
            transistors["stage2_gain_pmos"].current_source,
            "I_s2",
        )
        self.assertEqual(
            transistors["stage2_mirror_nmos"].multiplicity,
            2,
        )
        self.assertEqual(
            transistors["stage3_gain_nmos"].current_source,
            "I_s3",
        )
        self.assertEqual(
            transistors["feedforward_gain_pmos"].current_source,
            "I_s3",
        )
        self.assertEqual(pass_through, {"Cc1", "Cc2"})

    def test_critical_operating_points_include_feedforward_path(self):
        critical = get_topology("nmcf_three_stage").critical_operating_point_instances()

        self.assertIn("Mgmf2", critical)
        self.assertIn("Mmirror2a", critical)
        self.assertIn("Mmirror2b", critical)
        self.assertNotIn("Mload3", critical)


if __name__ == "__main__":
    unittest.main()
