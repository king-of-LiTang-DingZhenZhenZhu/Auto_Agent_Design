from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from models import DesignTarget, NetlistTemplate, format_spice_value, split_width
from config import Settings
from pdk_profiles import get_pdk_profile
from simulator import Simulator
from topologies import get_topology, get_topology_for_targets, list_topologies


class SpectreTopologyTest(unittest.TestCase):
    def test_split_width_uses_spectre_native_w_nf_m_semantics(self):
        instance_w, nf, m = split_width(12e-6, 2.6e-6)
        self.assertAlmostEqual(instance_w, 12e-6)
        self.assertEqual(nf, 5)
        self.assertEqual(m, 1)
        self.assertLessEqual(instance_w / nf, 2.6e-6)

        wide_w, wide_nf, wide_m = split_width(120e-6, 2.6e-6)
        self.assertEqual(wide_nf, 24)
        self.assertEqual(wide_m, 2)
        self.assertAlmostEqual(wide_w * wide_m, 120e-6)
        self.assertLessEqual(wide_w / wide_nf, 2.6e-6)

    def test_spice_value_format_never_uses_exponent_with_suffix(self):
        self.assertEqual(format_spice_value(1.0e-6), "1u")
        self.assertEqual(format_spice_value(1.3e-6), "1.3u")
        self.assertEqual(format_spice_value(999.999e-9), "999.999n")
        for value in (1.0e-6, 1.3e-6, 999.999e-9, 1.0e-9):
            formatted = format_spice_value(value)
            self.assertNotRegex(formatted, r"e[+-]\d+[munpfk]")

    def test_topology_metadata_uses_gbw_capability_names(self):
        for meta in list_topologies():
            self.assertGreaterEqual(meta.max_gbw_hz, meta.min_gbw_hz)
            self.assertEqual(meta.min_bw_hz, meta.min_gbw_hz)
            self.assertEqual(meta.max_bw_hz, meta.max_gbw_hz)

    def test_bandgap_hint_selects_bandgap_topology(self):
        self.assertEqual(
            get_topology_for_targets(DesignTarget(topology_hint="bandgap PTAT")),
            "bandgap_ptat",
        )

    def test_all_topologies_generate_native_spectre_projects(self):
        forbidden = [".lib ", ".options ", ".param ", ".subckt ", ".meas "]
        pdk = get_pdk_profile()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for meta in list_topologies():
                topo = get_topology(meta.name)
                project = topo.write_project(root / meta.name)
                circuit = (project / f"{meta.name}.cir").read_text(encoding="utf-8")
                file_names = [path.name for path in project.iterdir()]

                self.assertIn("simulator lang=spectre", circuit)
                self.assertIn(f'include "{pdk.spectre_model_path}"', circuit)
                self.assertIn(f"section={pdk.spectre_section}", circuit)
                self.assertIn("parameters ", circuit)
                self.assertRegex(circuit, rf"(?m)^subckt\s+\w+\s+\(")
                for token in forbidden:
                    self.assertNotIn(token, circuit)

                self.assertTrue(any(name.endswith(".scs") for name in file_names))
                self.assertFalse(any(name.endswith(".sp") for name in file_names))
                self.assertIn(f"tb_{meta.name}_sr.scs", file_names)
                self.assertIn(f"tb_{meta.name}_st.scs", file_names)

                ac_file = next(
                    project / name
                    for name in file_names
                    if name.endswith("_ac.scs")
                )
                ac_testbench = ac_file.read_text(encoding="utf-8")
                self.assertIn("outOpts options rawfmt=psfascii", ac_testbench)
                self.assertIn("soft_bin=allmodels", ac_testbench)
                self.assertIn("VCMsrc (vcm 0) vsource type=dc dc=VCM", ac_testbench)
                self.assertIn("VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1", ac_testbench)
                self.assertIn("Rfb (vout vinn) resistor r=1G", ac_testbench)
                self.assertIn("Cfb (vinn 0) capacitor c=1", ac_testbench)

                sr_testbench = (
                    project / f"tb_{meta.name}_sr.scs"
                ).read_text(encoding="utf-8")
                st_testbench = (
                    project / f"tb_{meta.name}_st.scs"
                ).read_text(encoding="utf-8")
                self.assertIn("srTran tran", sr_testbench)
                self.assertIn("stTran tran", st_testbench)
                self.assertIn("save vinp vout", sr_testbench)
                self.assertIn("save vinp vout", st_testbench)

    def test_topologies_use_pdk_profile_model_names(self):
        pdk = get_pdk_profile()

        five_t = get_topology("5t_ota").generate_circuit()
        self.assertIn(f") {pdk.pmos_model} w=Wtail", five_t)
        self.assertIn(f") {pdk.nmos_model} w=Wcm", five_t)

        folded = get_topology("folded_cascode").generate_circuit()
        self.assertIn(f") {pdk.pmos_lvt_model} l=Lbias", folded)
        self.assertIn(f") {pdk.nmos_lvt_model} l=Lbias", folded)

    def test_bandgap_ptat_embeds_frozen_folded_cascode_macro(self):
        topo = get_topology("bandgap_ptat")
        circuit = topo.generate_circuit()
        param_names = set(topo.get_param_space().get_param_names())

        self.assertRegex(circuit, r"(?m)^subckt bandgap_ptat \(vref vdd vss\)")
        self.assertRegex(
            circuit,
            r"(?m)^Xopamp \(nsense nfb vctrl opibias vdd vss\) folded_cascode",
        )
        self.assertIn("Port order: vip vin vout ibias vdd vss", circuit)
        self.assertIn("subckt folded_cascode (vip vin vout ibias vdd vss)", circuit)

        self.assertIn("Rptat", param_names)
        self.assertIn("Rctat", param_names)
        self.assertIn("BJT_AREA_RATIO", param_names)
        self.assertNotIn("Wdiffp", param_names)
        self.assertNotIn("Lbias", param_names)
        self.assertNotIn("m_half_unit", param_names)
        self.assertNotIn("bias_p_scale", param_names)

    def test_bandgap_ptat_uses_external_opamp_netlist(self):
        fake_opamp = """\
// fake folded opamp
subckt folded_cascode (vip vin vout ibias vdd vss)
Rfake (vout vss) resistor r=1G
ends folded_cascode
"""
        with tempfile.TemporaryDirectory() as tmp:
            opamp_path = Path(tmp) / "opamp.cir"
            opamp_path.write_text(fake_opamp, encoding="utf-8")

            circuit = get_topology("bandgap_ptat").generate_circuit(
                {"opamp_netlist": str(opamp_path)}
            )

        self.assertIn("// fake folded opamp", circuit)
        self.assertIn("Rfake (vout vss) resistor r=1G", circuit)
        self.assertIn(
            "Xopamp (nsense nfb vctrl opibias vdd vss) folded_cascode",
            circuit,
        )

    def test_bandgap_write_project_records_child_opamp_source(self):
        fake_opamp = """\
subckt folded_cascode (vip vin vout ibias vdd vss)
Rfake (vout vss) resistor r=1G
ends folded_cascode
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opamp_path = root / "optimized_opamp.cir"
            opamp_path.write_text(fake_opamp, encoding="utf-8")
            targets = DesignTarget(
                power_w=1e-3,
                load_cap_f=1e-12,
                custom_specs={"opamp_gain_db": 75, "vref_v": 1.2},
            )

            project = get_topology("bandgap_ptat").write_project(
                root / "bandgap",
                targets=targets,
                params={"opamp_netlist": str(opamp_path)},
                original_requirement="bandgap with optimized folded opamp",
            )
            req = json.loads((project / "requirements.json").read_text(encoding="utf-8"))
            child_circuit = (
                project / "child_blocks" / "folded_cascode_opamp" / "circuit.cir"
            )
            child_results = (
                project / "child_blocks" / "folded_cascode_opamp" / "source_results.json"
            )

            self.assertTrue(child_circuit.exists())
            self.assertTrue(child_results.exists())
            self.assertEqual(
                req["hierarchical_blocks"]["opamp"]["ports"],
                ["vip", "vin", "vout", "ibias", "vdd", "vss"],
            )
            self.assertEqual(
                req["hierarchical_blocks"]["opamp"]["sizing_policy"],
                "frozen_macro",
            )
            self.assertEqual(
                req["hierarchical_blocks"]["opamp"]["derived_targets"]["gain_db"],
                75.0,
            )

    def test_spectre_parameter_rendering_and_finger_split(self):
        topo = get_topology("5t_ota")
        circuit = topo.generate_circuit()
        rendered = NetlistTemplate.from_netlist(circuit).render(
            {
                "Wtail": 12e-6,
                "Ltail": 200e-9,
                "Wdp": 5e-6,
                "Ldp": 60e-9,
                "Wcm": 8e-6,
                "Lcm": 100e-9,
            },
            param_space=topo.get_param_space(),
            w_l_grid_step=10e-9,
        )

        self.assertIn("parameters Wtail=12u Ltail=200n", rendered)
        self.assertIn("Mtail (tail vbias vdd vdd) pch_mac w=12u l=200n nf=5 m=1", rendered)
        self.assertNotIn("w=Wtail", rendered)

    def test_wide_transistor_rendering_uses_m_after_nf_limit(self):
        topo = get_topology("two_stage_ota")
        circuit = topo.generate_circuit()
        rendered = NetlistTemplate.from_netlist(circuit).render(
            {
                "Wtail": 100e-6,
                "Ltail": 200e-9,
                "Wdiff": 5e-6,
                "Ldiff": 120e-9,
                "Wmirr": 5e-6,
                "Lmirr": 120e-9,
                "Wcs": 120e-6,
                "Wload": 110e-6,
                "Lload": 200e-9,
                "Cc": 1e-12,
                "Rz": 1e3,
            },
            param_space=topo.get_param_space(),
        )

        self.assertRegex(
            rendered,
            r"Mcs .* w=60u l=200n nf=24 m=2",
        )
        self.assertRegex(
            rendered,
            r"Mload .* w=55u l=200n nf=22 m=2",
        )

    def test_rendered_topology_values_do_not_emit_exponent_suffix(self):
        topo = get_topology("folded_cascode")
        circuit = topo.generate_circuit({"Wbp_small": 1.0e-6})
        self.assertNotRegex(circuit, r"e[+-]\d+[munpfk]")

        rendered = NetlistTemplate.from_netlist(circuit).render(
            {"Wcs": 1.0e-6, "Lbias": 330e-9},
            param_space=topo.get_param_space(),
            w_l_grid_step=10e-9,
        )
        self.assertNotRegex(rendered, r"e[+-]\d+[munpfk]")
        self.assertIn("w=1u", rendered)

    def test_dimensionless_scale_params_render_without_engineering_suffix(self):
        topo = get_topology("folded_cascode")
        rendered = NetlistTemplate.from_netlist(topo.generate_circuit()).render(
            {
                "bias_p_scale": 0.85,
                "bias_n_scale": 0.9,
                "bias_p_small_scale": 1.1,
                "bias_n_small_scale": 1.2,
            },
            param_space=topo.get_param_space(),
        )

        self.assertIn("parameters bias_p_scale=0.85 bias_n_scale=0.9", rendered)
        self.assertIn(
            "parameters bias_p_small_scale=1.1 bias_n_small_scale=1.2",
            rendered,
        )
        self.assertNotIn("bias_p_scale=850m", rendered)
        self.assertNotIn("bias_n_scale=900m", rendered)

    def test_folded_cascode_uses_bias_ratio_current_sources(self):
        topo = get_topology("folded_cascode")
        circuit = topo.generate_circuit()
        rendered = NetlistTemplate.from_netlist(circuit).render(
            topo.get_default_params(),
            param_space=topo.get_param_space(),
        )

        self.assertNotIn("Wtailp", rendered)
        self.assertNotIn("Wfoldn", rendered)
        self.assertNotIn("Wload", rendered)
        self.assertIn(
            "parameters bias_p_scale=1 bias_n_scale=1",
            rendered,
        )
        self.assertIn(
            "parameters bias_p_small_scale=1 bias_n_small_scale=1",
            rendered,
        )
        self.assertIn(
            "parameters Wbp_big=4.8u*Lbias/Lbias_ref*bias_p_scale",
            rendered,
        )
        self.assertIn(
            "Wbp_small=1.2u*Lbias/Lbias_ref*bias_p_scale*bias_p_small_scale",
            rendered,
        )
        self.assertIn(
            "parameters Wbn_big=4.8u*Lbias/Lbias_ref*bias_n_scale",
            rendered,
        )
        self.assertIn(
            "Wbn_small=1.2u*Lbias/Lbias_ref*bias_n_scale*bias_n_small_scale",
            rendered,
        )
        self.assertIn("parameters m_half_unit=2 m_load_ratio=2", rendered)
        self.assertIn(
            "Mtailp (ntail VB1 vdd vdd) pch_lvt_mac "
            "w=Wbp_big l=400n nf=nf_Wbp_big m=m_tail_unit*m_Wbp_big",
            rendered,
        )
        self.assertIn(
            "Mfold1 (nfold_l VB4 vss vss) nch_lvt_mac "
            "w=Wbn_big l=400n nf=nf_Wbn_big m=m_tail_unit*m_Wbn_big",
            rendered,
        )
        self.assertIn(
            "Mcasn1 (pmirr VB3 nfold_l vss) nch_lvt_mac "
            "w=Wbn_big l=400n nf=nf_Wbn_big m=m_half_unit*m_Wbn_big",
            rendered,
        )
        self.assertIn(
            "Mmirr1 (npm_l pmirr vdd vdd) pch_lvt_mac "
            "w=Wbp_big l=400n nf=nf_Wbp_big m=m_half_unit*m_Wbp_big",
            rendered,
        )
        self.assertIn(
            "Mcs (vout nstage1 vdd vdd) pch_lvt_mac "
            "w=30u l=400n nf=12 m=1",
            rendered,
        )
        self.assertIn(
            "Mload (vout VB4 vss vss) nch_lvt_mac "
            "w=Wbn_big l=400n nf=nf_Wbn_big m=m_load_unit*m_Wbn_big",
            rendered,
        )

        lscaled = NetlistTemplate.from_netlist(circuit).render(
            {
                "Lbias": 500e-9,
                "bias_p_scale": 1.2,
                "bias_n_scale": 0.85,
                "bias_p_small_scale": 1.1,
                "bias_n_small_scale": 0.9,
            },
            param_space=topo.get_param_space(),
        )
        self.assertIn("parameters Lbias=500n Lbias_ref=400n", lscaled)
        self.assertIn(
            "parameters bias_p_scale=1.2 bias_n_scale=0.85",
            lscaled,
        )
        self.assertIn(
            "parameters bias_p_small_scale=1.1 bias_n_small_scale=0.9",
            lscaled,
        )
        self.assertIn(
            "parameters Wbp_big=4.8u*Lbias/Lbias_ref*bias_p_scale",
            lscaled,
        )
        self.assertIn(
            "M9 (VB4 VB3 net3 vss) nch_lvt_mac l=500n w=Wbn_big",
            lscaled,
        )

    def test_write_project_uses_target_load_cap_for_testbenches(self):
        topo = get_topology("two_stage_ota")
        targets = DesignTarget(load_cap_f=1e-12)

        with tempfile.TemporaryDirectory() as tmp:
            project = topo.write_project(Path(tmp) / "two_stage", targets=targets)
            for suffix in ("ac", "sr", "st"):
                testbench = (
                    project / f"tb_two_stage_ota_{suffix}.scs"
                ).read_text(encoding="utf-8")
                self.assertIn("CL=1p", testbench)
                self.assertNotIn("CL=2p", testbench)

    def test_5t_vbias_is_testbench_owned_and_rendered(self):
        topo = get_topology("5t_ota")
        files = topo.get_circuit_files()
        self.assertNotIn("parameters VBIAS=", files.circuit_netlist)
        self.assertTrue(
            all("parameters VDD=" in tb and "VBIAS=" in tb for tb in files.testbenches)
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            simulator = Simulator(Settings(dry_run=True))
            tb_paths = simulator.render_circuit_and_testbench(
                NetlistTemplate.from_netlist(files.circuit_netlist),
                files.testbenches,
                {"VBIAS": 0.42},
                run_dir,
            )
            self.assertNotIn(
                "parameters VBIAS=",
                (run_dir / "circuit.cir").read_text(encoding="utf-8"),
            )
            for tb_path in tb_paths:
                self.assertIn(
                    "VBIAS=420m",
                    tb_path.read_text(encoding="utf-8"),
                )
            ac_testbench = tb_paths[0].read_text(encoding="utf-8")
            self.assertIn("// Diagnostic node saves", ac_testbench)
            self.assertIn("Xdut.tail", ac_testbench)

    def test_render_cleans_stale_run_directory_outputs(self):
        topo = get_topology("5t_ota")
        files = topo.get_circuit_files()

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "raw").mkdir()
            (run_dir / "raw" / "old.ac").write_text("stale", encoding="utf-8")
            (run_dir / "diagnostics").mkdir()
            (run_dir / "diagnostics" / "old.csv").write_text("stale", encoding="utf-8")
            (run_dir / "sim.log").write_text("stale", encoding="utf-8")

            simulator = Simulator(Settings(dry_run=True))
            simulator.render_circuit_and_testbench(
                NetlistTemplate.from_netlist(files.circuit_netlist),
                files.testbenches,
                {},
                run_dir,
            )

            self.assertTrue((run_dir / "circuit.cir").exists())
            self.assertFalse((run_dir / "raw").exists())
            self.assertFalse((run_dir / "diagnostics").exists())
            self.assertFalse((run_dir / "sim.log").exists())


if __name__ == "__main__":
    unittest.main()
