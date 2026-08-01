from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models import DesignTarget
from pdk_profiles import get_pdk_profile, validate_pdk_profile
from topologies import get_topology, get_topology_for_targets


class LeungMokSub1VBandgapTest(unittest.TestCase):
    def test_explicit_paper_hint_selects_topology(self):
        self.assertEqual(
            get_topology_for_targets(DesignTarget(
                topology_hint="Leung Mok 15-ppm bandgap"
            )),
            "leung_mok_sub1v_bandgap",
        )

    def test_fig3_core_and_compensation_connections(self):
        topology = get_topology("leung_mok_sub1v_bandgap")
        circuit = topology.generate_circuit()
        pdk = get_pdk_profile()

        self.assertRegex(
            circuit,
            r"(?m)^subckt leung_mok_sub1v_bandgap \(vref vdd vss\)",
        )
        self.assertIn("M1 (n3 vg vdd vb)", circuit)
        self.assertIn("M2 (n4 vg vdd vb)", circuit)
        self.assertIn("M3 (vref vg vdd vb)", circuit)
        self.assertIn("R2A1 (n3 n1) resistor r=R2_HIGH", circuit)
        self.assertIn("R2A2 (n1 vss) resistor r=R2_LOW", circuit)
        self.assertIn("R1Dev (n3 q1_e) resistor r=R1", circuit)
        self.assertIn(
            f"Q1 (vss vss q1_e) {pdk.resolve_model('pnp')} "
            "m=BJT_AREA_RATIO",
            circuit,
        )
        self.assertIn("R2B1 (n4 n2) resistor r=R2_HIGH", circuit)
        self.assertIn("Q2 (vss vss n4)", circuit)
        self.assertIn("R3Dev (vref vss) resistor r=R3", circuit)
        self.assertIn("CcompDev (vg n3) capacitor c=Ccomp", circuit)

    def test_fig3_startup_forward_bias_and_amplifier_connections(self):
        circuit = get_topology("leung_mok_sub1v_bandgap").generate_circuit()

        self.assertIn("MS1 (nstart vg vdd vb)", circuit)
        self.assertIn("MS2 (nstart vg vss vss)", circuit)
        self.assertIn("MS3 (n4 nstart vdd vb)", circuit)
        self.assertIn("MS4 (nbias_n nstart vdd vb)", circuit)
        self.assertIn("RSBDev (vdd vb) resistor r=RSB", circuit)
        self.assertIn("MSB (vb nbias_n vss vss)", circuit)

        self.assertIn("MA08 (ndiff_l n1 ndiff_tail vdd)", circuit)
        self.assertIn("MA09 (ndiff_r n2 ndiff_tail vdd)", circuit)
        self.assertIn("QA16 (vss nbase_l ndiff_l)", circuit)
        self.assertIn("QA17 (vss nbase_r ndiff_r)", circuit)
        self.assertIn("MA10 (nbase_l nbase_l vss vss)", circuit)
        self.assertIn("MA12 (nmirror_l nbase_l vss vss)", circuit)
        self.assertIn("MA14 (nmirror_l nmirror_l pcas_l vb)", circuit)
        self.assertIn("MA15 (vg nmirror_l pcas_r vb)", circuit)
        self.assertNotIn("subckt two_stage_ota", circuit)
        self.assertNotIn("subckt pmos_input_two_stage_ota", circuit)

    def test_paper_ratios_and_physical_search_contract(self):
        topology = get_topology("leung_mok_sub1v_bandgap")
        defaults = topology.get_default_params()
        params = {param.name: param for param in topology.get_param_space().params}

        r2 = defaults["R2_HIGH"] + defaults["R2_LOW"]
        self.assertAlmostEqual(r2 / defaults["R1"], 5.5)
        self.assertAlmostEqual(defaults["R3"] / r2, 0.48)
        self.assertEqual(defaults["BJT_AREA_RATIO"], 64)
        self.assertEqual(params["BJT_AREA_RATIO"].value_type, "int")
        self.assertIsNone(topology.get_gmid_spec())
        self.assertEqual(topology.get_hierarchical_blocks(), [])
        self.assertEqual(
            topology.required_model_roles(),
            ("nmos", "pmos", "pnp"),
        )
        self.assertEqual(
            validate_pdk_profile(
                required_model_roles=topology.required_model_roles(),
                require_gmid=True,
            ),
            [],
        )

    def test_dedicated_testbenches_use_paper_ranges(self):
        topology = get_topology("leung_mok_sub1v_bandgap")
        files = topology.get_circuit_files()

        self.assertEqual(
            files.testbench_suffixes,
            ["startup", "psrr", "temperature", "line"],
        )
        for testbench in files.testbenches:
            self.assertIn(
                "Xdut (vout vdd vss) leung_mok_sub1v_bandgap",
                testbench,
            )
        self.assertIn("parameters VDD=1.0", files.testbenches[0])
        self.assertIn("start=0.0 stop=100.0", files.testbenches[2])
        self.assertIn("VDD_MIN=0.98 VDD_MAX=1.1", files.testbenches[3])

    def test_project_has_no_frozen_opamp_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = get_topology("leung_mok_sub1v_bandgap").write_project(
                Path(tmp) / "leung_mok",
                targets=DesignTarget(
                    vref_v=0.603,
                    tempco_ppm_per_c=15,
                    power_w=18e-6,
                    topology_hint="Leung Mok sub-1-V bandgap",
                ),
            )

            self.assertFalse((project / "hierarchy.json").exists())
            self.assertTrue(
                (project / "tb_leung_mok_sub1v_bandgap_startup.scs").exists()
            )


if __name__ == "__main__":
    unittest.main()
