from __future__ import annotations

import unittest

from models import DesignTarget
from topologies import get_topology, get_topology_for_targets


class MZCTwoStageOTATest(unittest.TestCase):
    def test_nmos_input_mzc_matches_figure_one_c_signal_polarity(self):
        topology = get_topology("mzc_two_stage_ota")
        circuit = topology.generate_circuit()
        testbench = topology.generate_testbench(analysis_type="ac")
        physical_params = set(topology.get_param_space().get_param_names())
        gmid_params = set(topology.get_gmid_spec().build_param_space().get_param_names())

        self.assertIn(
            "subckt mzc_two_stage_ota (vip vin vout ibias vdd vss)",
            circuit,
        )
        self.assertIn("Mdiff2 (n_s1 vip n_tail vss)", circuit)
        self.assertIn("Mffdiff1 (n_ff_mirr vip n_ff_tail vss)", circuit)
        self.assertIn("Mffdiff2 (vout vin n_ff_tail vss)", circuit)
        self.assertIn("Mtailff (n_ff_tail ibias vss vss)", circuit)
        self.assertIn("Cc (n_s1 vout) capacitor c=Cc", circuit)
        self.assertNotIn("Rz (", circuit)
        self.assertNotIn("Rz", physical_params)
        self.assertNotIn("Rz", gmid_params)
        self.assertIn("fts_ratio", physical_params)
        self.assertIn("fts_ratio", gmid_params)
        self.assertIn(
            "Xdut (vinp vinn vout ibias vdd vss) mzc_two_stage_ota",
            testbench,
        )

    def test_pmos_input_mzc_reverses_fts_inputs_relative_to_first_stage(self):
        topology = get_topology("pmos_input_mzc_two_stage_ota")
        circuit = topology.generate_circuit({"fts_ratio": 1.1})

        self.assertIn(
            "subckt pmos_input_mzc_two_stage_ota (vip vin vout ibias vdd vss)",
            circuit,
        )
        self.assertIn("Mdiff1 (n_mirr vin n_tail vdd)", circuit)
        self.assertIn("Mdiff2 (n_s1 vip n_tail vdd)", circuit)
        self.assertIn("Mffdiff1 (n_ff_mirr vip n_ff_tail vdd)", circuit)
        self.assertIn("Mffdiff2 (vout vin n_ff_tail vdd)", circuit)
        self.assertIn("Mtailff (n_ff_tail ibias vdd vdd)", circuit)
        self.assertIn("parameters Cc=500f fts_ratio=1.1", circuit)
        self.assertIn("Cc (n_s1 vout) capacitor c=Cc", circuit)
        self.assertNotIn("Rz (", circuit)
        self.assertEqual(topology.required_model_roles(), ("nmos_lvt", "pmos_lvt"))

    def test_topology_hints_select_mzc_input_polarity(self):
        self.assertEqual(
            get_topology_for_targets(DesignTarget(topology_hint="MZC FTS two-stage")),
            "mzc_two_stage_ota",
        )
        self.assertEqual(
            get_topology_for_targets(
                DesignTarget(topology_hint="PMOS-input feedforward two-stage")
            ),
            "pmos_input_mzc_two_stage_ota",
        )


if __name__ == "__main__":
    unittest.main()
