from __future__ import annotations

import unittest

from models import DesignTarget
from topologies import get_topology, get_topology_for_targets


class NMCNRThreeStageTest(unittest.TestCase):
    def test_structure_matches_leung_nmcnr_figure_one_e(self):
        topology = get_topology("nmcnr_three_stage")
        circuit = topology.generate_circuit()
        params = set(topology.get_param_space().get_param_names())

        self.assertIn("Mdiff1a (s1_mirr vin tail vdd)", circuit)
        self.assertIn("Mgm2 (s2_mirr s1_out vdd vdd)", circuit)
        self.assertIn("Mmirror2b (s2_out s2_mirr vss vss)", circuit)
        self.assertIn("Mgm3 (vout s2_out vss vss)", circuit)
        self.assertIn("Mload3 (vout vbiasp vdd vdd)", circuit)
        self.assertIn("Cc1 (s1_out vout) capacitor c=Cc1", circuit)
        self.assertIn("Cc2 (s2_out n_rm) capacitor c=Cc2", circuit)
        self.assertIn("RmDev (n_rm vout) resistor r=Rm", circuit)

        self.assertNotIn("Mgmf1", circuit)
        self.assertNotIn("Mgmf2", circuit)
        self.assertNotIn("Wgmf1", params)
        self.assertNotIn("Wgmf2", params)
        self.assertTrue({"Cc1", "Cc2", "Rm"}.issubset(params))
        self.assertEqual(
            topology.required_model_roles(),
            ("nmos_lvt", "pmos_lvt"),
        )

    def test_gmid_contract_has_no_feedforward_branch(self):
        spec = get_topology("nmcnr_three_stage").get_gmid_spec()
        branches = {branch.name for branch in spec.branch_currents}
        roles = {transistor.role for transistor in spec.transistors}
        pass_through = {param.name for param in spec.pass_through_params}

        self.assertNotIn("I_f1", branches)
        self.assertFalse(any(role.startswith("feedforward_") for role in roles))
        self.assertIn("stage3_gain_nmos", roles)
        self.assertIn("stage3_load_pmos", roles)
        self.assertEqual(pass_through, {"Cc1", "Cc2", "Rm"})

    def test_testbenches_and_hint_use_nmcnr_subcircuit(self):
        topology = get_topology("nmcnr_three_stage")

        for analysis in ("ac", "sr", "st"):
            testbench = topology.generate_testbench(analysis_type=analysis)
            self.assertIn(") nmcnr_three_stage", testbench)
            self.assertNotIn(") mnmc_three_stage", testbench)

        self.assertEqual(
            get_topology_for_targets(
                DesignTarget(topology_hint="NMCNR three-stage OTA")
            ),
            "nmcnr_three_stage",
        )
        self.assertEqual(
            get_topology_for_targets(
                DesignTarget(topology_hint="nested Miller nulling resistor")
            ),
            "nmcnr_three_stage",
        )

    def test_critical_operating_points_exclude_feedforward_devices(self):
        critical = get_topology(
            "nmcnr_three_stage"
        ).critical_operating_point_instances()

        self.assertIn("Mgm3", critical)
        self.assertIn("Mload3", critical)
        self.assertNotIn("Mgmf1a", critical)
        self.assertNotIn("Mgmf2", critical)


if __name__ == "__main__":
    unittest.main()
