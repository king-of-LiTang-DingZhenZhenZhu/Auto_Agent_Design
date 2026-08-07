from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from passive_realization import (
    realize_passives,
    realize_project_passives,
    solve_passive,
)
from pdk_passive_probe import render_cdf_probe
from pdk_profiles import (
    PassiveDeviceProfile,
    get_pdk_profile,
    validate_pdk_profile,
)
from models import SimResult
from topologies import get_topology
from virtuoso_export.exporter import load_device_map
from virtuoso_export.parser import parse_netlist


class PassiveRealizationTests(unittest.TestCase):
    def test_formula_resistor_snaps_to_legal_geometry(self):
        device = self._resistor()

        candidate = solve_passive(10_000.0, device)

        self.assertAlmostEqual(candidate.achieved_value, 10_000.0)
        self.assertEqual(candidate.params["w"], "1u")
        self.assertEqual(candidate.params["l"], "100u")

    def test_formula_capacitor_uses_parallel_units_when_unit_area_is_limited(self):
        device = replace(
            self._capacitor(),
            max_width_m=10e-6,
            max_length_m=10e-6,
            max_unit_area_m2=100e-12,
            max_parallel_units=4,
        )

        candidate = solve_passive(200e-15, device)

        self.assertEqual(candidate.parallel_units, 2)
        self.assertAlmostEqual(candidate.achieved_value, 200e-15)

    def test_lookup_mapping_and_series_segmentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "poly.json"
            table.write_text(
                json.dumps({"version": "unit", "points": [
                    {"value": 5_000.0, "params": {"w": "1u", "l": "50u"}, "area_m2": 50e-12}
                ]}),
                encoding="utf-8",
            )
            device = replace(
                self._resistor(),
                mapping_mode="lookup",
                lookup_table_path=str(table),
                max_series_units=2,
                value_tolerance=0.001,
            )

            candidate = solve_passive(10_000.0, device)

            self.assertEqual(candidate.series_units, 2)
            self.assertEqual(candidate.achieved_value, 10_000.0)

    def test_realize_two_stage_passives_but_preserve_testbench_only_devices(self):
        profile = self._profile()
        topology = get_topology("two_stage_ota")
        netlist = topology.generate_circuit({"Rz": 10_000.0, "Cc": 200e-15})

        realized, records = realize_passives(
            netlist,
            topology.passive_implementations(),
            profile,
        )

        self.assertIn("Rz (n_s1 n_rz) unit_rpoly", realized)
        self.assertIn("Cc (n_rz vout) unit_mim", realized)
        self.assertNotIn("Rz (n_s1 n_rz) resistor", realized)
        self.assertEqual({record.instance for record in records}, {"Rz", "Cc"})

    def test_undeclared_ideal_dut_passive_is_rejected(self):
        netlist = """simulator lang=spectre
subckt unit (a b)
R1 (a b) resistor r=1k
ends unit
"""
        with self.assertRaisesRegex(ValueError, "without implementation metadata"):
            realize_passives(netlist, (), self._profile())

    def test_profile_validation_rejects_incomplete_passive_mapping(self):
        bad = replace(self._resistor(), sheet_resistance_ohm_per_square=None)
        profile = replace(
            get_pdk_profile(),
            passive_devices={"bad": bad},
            passive_role_map={"feedback_resistor": "missing"},
        )

        errors = validate_pdk_profile(profile)

        self.assertTrue(any("sheet resistance" in error for error in errors))
        self.assertTrue(any("unknown device 'missing'" in error for error in errors))

    def test_external_profile_resolves_relative_versioned_lookup_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table = root / "characterization" / "rpoly.json"
            table.parent.mkdir()
            table.write_text(
                json.dumps({
                    "version": "unit-v1",
                    "corner": "tt",
                    "temperature_c": 27,
                    "method": "DC V/I",
                    "points": [{"value": 1000.0, "params": {"w": "1u", "l": "10u"}}],
                }),
                encoding="utf-8",
            )
            profile_data = get_pdk_profile().to_dict()
            profile_data["passive_devices"] = {
                "rpoly": {
                    "kind": "resistor",
                    "spectre_model": "unit_rpoly",
                    "virtuoso_lib": "unitTech",
                    "virtuoso_cell": "rpoly",
                    "mapping_mode": "lookup",
                    "lookup_table_path": "characterization/rpoly.json",
                }
            }
            profile_data["passive_role_map"] = {"feedback_resistor": "rpoly"}
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile_data), encoding="utf-8")

            profile = get_pdk_profile(str(profile_path))
            errors = validate_pdk_profile(profile)

            self.assertEqual(
                Path(profile.passive_devices["rpoly"].lookup_table_path),
                table.resolve(),
            )
            self.assertFalse([error for error in errors if "passive" in error])

    def test_parser_and_device_map_keep_pdk_two_terminal_devices(self):
        profile = self._profile()
        netlist = """simulator lang=spectre
subckt unit (a b c)
R1 (a b) unit_rpoly w=1u l=10u
C1 (b c) unit_mim w=2u l=5u
ends unit
"""
        with patch("pdk_profiles.get_pdk_profile", return_value=profile), patch(
            "virtuoso_export.models.get_pdk_profile", return_value=profile
        ):
            ir = parse_netlist(netlist)
            device_map = load_device_map()

        self.assertEqual([item.model for item in ir.instances], ["unit_rpoly", "unit_mim"])
        self.assertEqual(device_map["unit_rpoly"].cell, "rpoly")
        self.assertEqual(device_map["unit_mim"].cell, "mimcap")

    def test_cdf_probe_uses_configured_pcell_without_modifying_it(self):
        with patch("pdk_passive_probe.get_pdk_profile", return_value=self._profile()):
            skill = render_cdf_probe("unit_mim", "report.txt")

        self.assertIn('ddGetObj("unitTech" "mimcap")', skill)
        self.assertIn("cdfGetBaseCellCDF", skill)
        self.assertIn("master~>terminals", skill)
        self.assertNotIn("dbCreateInst", skill)

    def test_project_realization_requires_and_records_pdk_nominal_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            netlist = project / "netlist" / "circuit.cir"
            simulation = project / "simulation"
            netlist.parent.mkdir(parents=True)
            simulation.mkdir()
            topology = get_topology("two_stage_ota")
            netlist.write_text(topology.generate_circuit(), encoding="utf-8")
            (simulation / "tb_circuit.scs").write_text(
                topology.generate_testbench(), encoding="utf-8"
            )
            results = project / "results.json"
            results.write_text(
                json.dumps({
                    "topology_name": "two_stage_ota",
                    "all_targets_met": True,
                    "netlist_file": str(netlist),
                    "targets": {"gain_db": 40.0},
                }),
                encoding="utf-8",
            )

            with patch(
                "simulator.Simulator.run_all_testbenches",
                return_value=SimResult(gain_db=60.0, converged=True),
            ):
                report = realize_project_passives(
                    results, simulate=True, profile=self._profile()
                )

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["verified"])
            physical = Path(report["netlist_file"])
            self.assertIn("unit_rpoly", physical.read_text(encoding="utf-8"))
            persisted = json.loads(
                (project / "passive_realization" / "passive_realization.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(persisted["nominal_pass"])

    @staticmethod
    def _resistor() -> PassiveDeviceProfile:
        return PassiveDeviceProfile(
            kind="resistor",
            spectre_model="unit_rpoly",
            virtuoso_lib="unitTech",
            virtuoso_cell="rpoly",
            mapping_mode="formula",
            min_width_m=1e-6,
            max_width_m=100e-6,
            min_length_m=1e-6,
            max_length_m=1e-3,
            geometry_grid_m=0.1e-6,
            max_unit_area_m2=1e-7,
            max_series_units=4,
            max_parallel_units=4,
            value_tolerance=0.001,
            sheet_resistance_ohm_per_square=100.0,
        )

    @staticmethod
    def _capacitor() -> PassiveDeviceProfile:
        return PassiveDeviceProfile(
            kind="capacitor",
            spectre_model="unit_mim",
            virtuoso_lib="unitTech",
            virtuoso_cell="mimcap",
            mapping_mode="formula",
            min_width_m=1e-6,
            max_width_m=100e-6,
            min_length_m=1e-6,
            max_length_m=1e-3,
            geometry_grid_m=0.1e-6,
            max_unit_area_m2=1e-7,
            max_series_units=4,
            max_parallel_units=4,
            value_tolerance=0.001,
            capacitance_per_area_f_per_m2=1e-3,
        )

    def _profile(self):
        return replace(
            get_pdk_profile(),
            passive_devices={
                "unit_rpoly": self._resistor(),
                "unit_mim": self._capacitor(),
            },
            passive_role_map={
                "compensation_resistor": "unit_rpoly",
                "compensation_capacitor": "unit_mim",
            },
        )


if __name__ == "__main__":
    unittest.main()
