from __future__ import annotations

import unittest

from topologies import get_topology
from virtuoso_export.models import DEFAULT_DEVICE_MAP
from virtuoso_export.parser import parse_netlist
from virtuoso_export.skill_writer import write_skill


class VirtuosoExportTest(unittest.TestCase):
    def test_parse_folded_cascode_instances_and_ports(self):
        netlist = get_topology("folded_cascode").generate_circuit()

        ir = parse_netlist(netlist)

        self.assertEqual(ir.subckt_name, "folded_cascode")
        self.assertEqual(ir.ports, ["vip", "vin", "vout", "ibias", "vdd", "vss"])

        mos_instances = [inst for inst in ir.instances if inst.kind == "mos"]
        self.assertEqual(len(mos_instances), 26)

        mtailp = next(inst for inst in ir.instances if inst.name == "Mtailp")
        self.assertEqual(mtailp.model, "pch_lvt_mac")
        self.assertEqual(mtailp.nodes, ["ntail", "VB1", "vdd", "vdd"])
        self.assertEqual(mtailp.params["W"], "Wtailp")
        self.assertEqual(mtailp.params["L"], "Ltailp")
        self.assertEqual(mtailp.params["nf"], "1")

    def test_parse_resistor_and_capacitor(self):
        netlist = get_topology("folded_cascode").generate_circuit()

        ir = parse_netlist(netlist)

        rz = next(inst for inst in ir.instances if inst.name == "Rz")
        cc = next(inst for inst in ir.instances if inst.name == "Cc")
        self.assertEqual(rz.kind, "res")
        self.assertEqual(rz.nodes, ["nstage1", "n_rz"])
        self.assertEqual(rz.params["R"], "Rz")
        self.assertEqual(cc.kind, "cap")
        self.assertEqual(cc.nodes, ["n_rz", "vout"])
        self.assertEqual(cc.params["C"], "Cc")

    def test_skill_writer_contains_target_and_instances(self):
        ir = parse_netlist(get_topology("5t_ota").generate_circuit())

        skill = write_skill(
            ir,
            DEFAULT_DEVICE_MAP,
            lib_name="BO_Designs",
            cell_name="ota_5t_opt",
        )

        self.assertIn('libName = "BO_Designs"', skill)
        self.assertIn('cellName = "ota_5t_opt"', skill)
        self.assertIn('dbCreateInst(cv master "Mtail"', skill)
        self.assertIn('dbCreateInst(cv master "Mdp1"', skill)
        for port in ["vip", "vin", "vout", "vbias", "vdd", "vss"]:
            self.assertIn(f'dbCreateTerm(net "{port}"', skill)

    def test_missing_device_map_fails_before_writing_skill(self):
        ir = parse_netlist(get_topology("5t_ota").generate_circuit())
        incomplete_map = {"res": DEFAULT_DEVICE_MAP["res"]}

        with self.assertRaisesRegex(ValueError, "Device map is missing"):
            write_skill(ir, incomplete_map, lib_name="BO_Designs", cell_name="bad")


if __name__ == "__main__":
    unittest.main()
