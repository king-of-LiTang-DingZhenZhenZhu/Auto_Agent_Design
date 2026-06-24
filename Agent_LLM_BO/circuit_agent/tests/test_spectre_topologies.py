from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models import DesignTarget, NetlistTemplate
from config import Settings
from simulator import Simulator
from topologies import get_topology, list_topologies


class SpectreTopologyTest(unittest.TestCase):
    def test_topology_metadata_uses_gbw_capability_names(self):
        for meta in list_topologies():
            self.assertGreaterEqual(meta.max_gbw_hz, meta.min_gbw_hz)
            self.assertEqual(meta.min_bw_hz, meta.min_gbw_hz)
            self.assertEqual(meta.max_bw_hz, meta.max_gbw_hz)

    def test_all_topologies_generate_native_spectre_projects(self):
        forbidden = [".lib ", ".options ", ".param ", ".subckt ", ".meas "]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for meta in list_topologies():
                topo = get_topology(meta.name)
                project = topo.write_project(root / meta.name)
                circuit = (project / f"{meta.name}.cir").read_text(encoding="utf-8")
                file_names = [path.name for path in project.iterdir()]

                self.assertIn("simulator lang=spectre", circuit)
                self.assertIn('include "/PDKS/TSMC28nm/models/spectre/toplevel.scs"', circuit)
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

        self.assertIn("parameters Wtail=2.4u Ltail=200n", rendered)
        self.assertIn("Mtail (tail vbias vdd vdd) pch_mac w=2.4u l=200n nf=5 m=1", rendered)
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
            r"Mcs .* w=2\.5u l=200n nf=24 m=2",
        )
        self.assertRegex(
            rendered,
            r"Mload .* w=2\.5u l=200n nf=22 m=2",
        )

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
        self.assertIn("parameters m_half_unit=2 m_load_extra=0", rendered)
        self.assertIn(
            "Mtailp (ntail VB1 vdd vdd) pch_lvt_mac "
            "w=2.4u l=400n nf=nf_Wbp_big m=m_tail_unit*m_Wbp_big",
            rendered,
        )
        self.assertIn(
            "Mfold1 (nfold_l VB4 vss vss) nch_lvt_mac "
            "w=1.2u l=400n nf=nf_Wbn_big m=m_tail_unit*m_Wbn_big",
            rendered,
        )
        self.assertIn(
            "Mcasn1 (pmirr VB3 nfold_l vss) nch_lvt_mac "
            "w=1.2u l=400n nf=nf_Wbn_big m=m_half_unit*m_Wbn_big",
            rendered,
        )
        self.assertIn(
            "Mmirr1 (npm_l pmirr vdd vdd) pch_lvt_mac "
            "w=2.4u l=400n nf=nf_Wbp_big m=m_half_unit*m_Wbp_big",
            rendered,
        )
        self.assertIn(
            "Mcs (vout nstage1 vdd vdd) pch_lvt_mac "
            "w=2.5u l=400n nf=12 m=1",
            rendered,
        )
        self.assertIn(
            "Mload (vout VB4 vss vss) nch_lvt_mac "
            "w=1.2u l=400n nf=nf_Wbn_big m=m_load_unit*m_Wbn_big",
            rendered,
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


if __name__ == "__main__":
    unittest.main()
