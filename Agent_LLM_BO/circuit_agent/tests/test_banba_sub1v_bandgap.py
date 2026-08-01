from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from knowledge_review import build_knowledge_analysis
from models import DesignTarget, NetlistTemplate
from pdk_profiles import get_pdk_profile
from topologies import get_topology, get_topology_for_targets


class BanbaSub1VBandgapTest(unittest.TestCase):
    def test_explicit_hint_selects_banba_topology(self):
        self.assertEqual(
            get_topology_for_targets(
                DesignTarget(topology_hint="Banba sub-1-V bandgap")
            ),
            "banba_sub1v_bandgap",
        )

    def test_circuit_reproduces_paper_current_summing_core(self):
        circuit = get_topology("banba_sub1v_bandgap").generate_circuit()

        self.assertRegex(
            circuit,
            r"(?m)^subckt banba_sub1v_bandgap \(vref vdd vss\)",
        )
        self.assertIn(
            "parameters R12=2.063meg PTAT_WEIGHT=11.1612 VREF_SCALE=412m",
            circuit,
        )
        self.assertIn(
            "parameters R3=R12/PTAT_WEIGHT R4=R12*VREF_SCALE "
            "DIODE_AREA_RATIO=8",
            circuit,
        )
        self.assertIn("P1 (va vg vdd vdd)", circuit)
        self.assertIn("P2 (vb vg vdd vdd)", circuit)
        self.assertIn("P3 (vref vg vdd vdd)", circuit)
        self.assertIn("R1dev (va vss) resistor r=R12", circuit)
        self.assertIn("R2dev (vb vss) resistor r=R12", circuit)
        self.assertIn("R3dev (vb vdn) resistor r=R3", circuit)
        self.assertIn("QN (vss vss vdn)", circuit)
        self.assertIn("m=DIODE_AREA_RATIO", circuit)
        self.assertIn("R4dev (vref vss) resistor r=R4", circuit)
        self.assertIn(
            "Xopamp (vb va vg opibias vdd vss) two_stage_ota",
            circuit,
        )
        self.assertIn("subckt two_stage_ota", circuit)
        self.assertIn(
            f"Mdiff1 (n_mirr vin n_tail vss) {get_pdk_profile().nmos_model}",
            circuit,
        )
        self.assertIn(
            "IOPBIASsrc (vdd opibias) isource type=dc dc=Iopbias",
            circuit,
        )

    def test_dedicated_bandgap_testbenches_use_new_subckt(self):
        topology = get_topology("banba_sub1v_bandgap")
        files = topology.get_circuit_files()

        self.assertIsNone(topology.get_gmid_spec())
        self.assertEqual(
            [param.name for param in topology.get_param_space().params],
            ["R12", "PTAT_WEIGHT", "VREF_SCALE", "Lmirror_p"],
        )
        self.assertEqual(
            files.testbench_suffixes,
            ["startup", "psrr", "temperature", "line"],
        )
        for testbench in files.testbenches:
            self.assertIn(
                "Xdut (vout vdd vss) banba_sub1v_bandgap",
                testbench,
            )
        self.assertIn("startupTran tran", files.testbenches[0])
        self.assertIn("psrrAC ac", files.testbenches[1])
        self.assertIn("tempSweep dc", files.testbenches[2])
        self.assertIn("lineSweep dc", files.testbenches[3])

    def test_bo_render_preserves_derived_resistor_relations(self):
        topology = get_topology("banba_sub1v_bandgap")
        rendered = NetlistTemplate.from_netlist(
            topology.generate_circuit()
        ).render(
            {
                "R12": 2e6,
                "PTAT_WEIGHT": 12.0,
                "VREF_SCALE": 0.4,
                "Lmirror_p": 500e-9,
            },
            param_space=topology.get_param_space(),
        )

        self.assertIn(
            "parameters R12=2meg PTAT_WEIGHT=12 VREF_SCALE=0.4",
            rendered,
        )
        self.assertIn(
            "parameters R3=R12/PTAT_WEIGHT R4=R12*VREF_SCALE "
            "DIODE_AREA_RATIO=8",
            rendered,
        )

    def test_project_records_nmos_input_child_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = get_topology("banba_sub1v_bandgap").write_project(
                Path(tmp) / "banba",
                targets=DesignTarget(
                    vref_v=0.515,
                    power_w=10e-6,
                    topology_hint="Banba sub-1-V bandgap",
                ),
                original_requirement="Implement Banba et al. sub-1-V BGR",
            )
            hierarchy = json.loads(
                (project / "hierarchy.json").read_text(encoding="utf-8")
            )
            requirements = json.loads(
                (project / "requirements.json").read_text(encoding="utf-8")
            )

        child = hierarchy["blocks"][0]
        self.assertEqual(hierarchy["parent_topology"], "banba_sub1v_bandgap")
        self.assertEqual(child["topology_name"], "two_stage_ota")
        self.assertEqual(child["expected_subckt"], "two_stage_ota")
        self.assertEqual(child["netlist_param"], "opamp_netlist")
        self.assertEqual(child["custom_specs"]["input_common_mode_v"], 0.7)
        self.assertEqual(
            child["custom_specs"]["input_common_mode_min_v"],
            0.65,
        )
        self.assertEqual(
            child["custom_specs"]["input_common_mode_max_v"],
            0.75,
        )
        self.assertEqual(requirements["targets"]["vref_v"], 0.515)

    def test_knowledge_review_derives_paper_ratios(self):
        analysis = build_knowledge_analysis(
            "banba_sub1v_bandgap",
            history={"targets": {}},
            records=[{
                "iteration": 1,
                "params": {
                    "DIODE_AREA_RATIO": 8,
                    "R12": 2.063e6,
                    "PTAT_WEIGHT": 11.1612,
                    "VREF_SCALE": 0.412,
                },
                "result": {},
            }],
            workspace="unused",
        )

        derived = analysis["run_analyses"][0]["derived"]
        self.assertAlmostEqual(
            derived["delta_vbe_27c_first_order_V"],
            25.852e-3 * 2.079441542,
        )
        self.assertAlmostEqual(
            derived["r4_over_r12_first_order"],
            0.412,
        )
        self.assertAlmostEqual(
            derived["r12_over_r3_first_order"],
            11.1612,
        )


if __name__ == "__main__":
    unittest.main()
