from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models import NetlistTemplate
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

                ac_file = next(
                    project / name
                    for name in file_names
                    if name.endswith("_ac.scs")
                )
                ac_testbench = ac_file.read_text(encoding="utf-8")
                self.assertIn("outOpts options rawfmt=psfascii", ac_testbench)
                self.assertIn("VCMsrc (vcm 0) vsource type=dc dc=VCM", ac_testbench)
                self.assertIn("VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1", ac_testbench)
                self.assertIn("Rfb (vout vinn) resistor r=1G", ac_testbench)
                self.assertIn("Cfb (vinn 0) capacitor c=1", ac_testbench)

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

        self.assertIn("parameters Wtail=3u Ltail=200n", rendered)
        self.assertIn("Mtail (tail vbias vdd vdd) pch_mac w=3u l=200n nf=4", rendered)
        self.assertNotIn("w=Wtail", rendered)


if __name__ == "__main__":
    unittest.main()
