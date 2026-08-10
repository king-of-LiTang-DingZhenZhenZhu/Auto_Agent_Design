from __future__ import annotations

import unittest

from models import DesignTarget
from pdk_integration.profiles import get_pdk_profile
from topologies import get_topology, get_topology_for_targets


class MNMCThreeStageTest(unittest.TestCase):
    def test_structure_matches_leung_mnmc_figure_one_f(self):
        topology = get_topology("mnmc_three_stage")
        circuit = topology.generate_circuit()
        params = set(topology.get_param_space().get_param_names())
        pdk = get_pdk_profile()

        self.assertIn("Mdiff1a (s1_mirr vin tail vdd)", circuit)
        self.assertIn("Mdiff1b (s1_out vip tail vdd)", circuit)
        self.assertIn("Mgm2 (s2_mirr s1_out vdd vdd)", circuit)
        self.assertIn("Mmirror2b (s2_out s2_mirr vss vss)", circuit)
        self.assertIn("Mgmf1a (f1_mirr vin f1_tail vdd)", circuit)
        self.assertIn("Mgmf1b (s2_out vip f1_tail vdd)", circuit)
        self.assertIn(
            "Mloadf1b (s2_out f1_mirr vss vss) " + pdk.nmos_lvt_model,
            circuit,
        )
        self.assertIn("Mgm3 (vout s2_out vss vss)", circuit)
        self.assertIn("Mload3 (vout vbiasp vdd vdd)", circuit)
        self.assertIn("Cc1 (s1_out vout) capacitor c=Cc1", circuit)
        self.assertIn("Cc2 (s2_out vout) capacitor c=Cc2", circuit)

        self.assertNotIn("Mgmf2", circuit)
        self.assertNotIn("Rz", circuit)
        self.assertNotIn("Wgmf2", params)
        self.assertTrue(
            {
                "Wload3", "Wtailf1", "Wgmf1", "Wloadf1", "Cc1", "Cc2",
            }.issubset(params)
        )
        self.assertAlmostEqual(
            next(
                param.high
                for param in topology.get_param_space().params
                if param.name == "Cc2"
            ),
            100e-12,
        )
        self.assertEqual(
            topology.required_model_roles(),
            ("nmos_lvt", "pmos_lvt"),
        )

    def test_gmid_contract_models_feedforward_differential_stage(self):
        spec = get_topology("mnmc_three_stage").get_gmid_spec()
        branches = {branch.name for branch in spec.branch_currents}
        transistors = {transistor.role: transistor for transistor in spec.transistors}
        pass_through = {param.name for param in spec.pass_through_params}

        self.assertIn("I_f1", branches)
        self.assertAlmostEqual(
            next(
                branch.default
                for branch in spec.branch_currents
                if branch.name == "I_f1"
            ),
            270e-6,
        )
        self.assertNotIn("feedforward_gain_pmos", transistors)
        self.assertEqual(
            transistors["feedforward_tail_pmos"].current_source,
            "I_f1",
        )
        self.assertEqual(
            transistors["feedforward_diff_pmos"].multiplicity,
            2,
        )
        self.assertEqual(
            transistors["feedforward_load_nmos"].multiplicity,
            2,
        )
        self.assertEqual(
            transistors["stage3_load_pmos"].current_source,
            "I_s3",
        )
        self.assertEqual(pass_through, {"Cc1", "Cc2"})

    def test_testbenches_and_hint_use_mnmc_subcircuit(self):
        topology = get_topology("mnmc_three_stage")

        for analysis in ("ac", "sr", "st"):
            testbench = topology.generate_testbench(analysis_type=analysis)
            self.assertIn(
                "Xdut (vinp ",
                testbench,
            )
            self.assertIn(") mnmc_three_stage", testbench)
            self.assertNotIn(") nmcf_three_stage", testbench)

        self.assertEqual(
            get_topology_for_targets(
                DesignTarget(topology_hint="MNMC feedforward three-stage OTA")
            ),
            "mnmc_three_stage",
        )

    def test_critical_operating_points_cover_fts_and_output_load(self):
        critical = get_topology(
            "mnmc_three_stage"
        ).critical_operating_point_instances()

        self.assertIn("Mgmf1a", critical)
        self.assertIn("Mgmf1b", critical)
        self.assertIn("Mloadf1a", critical)
        self.assertIn("Mloadf1b", critical)
        self.assertIn("Mload3", critical)
        self.assertNotIn("Mgmf2", critical)


if __name__ == "__main__":
    unittest.main()
