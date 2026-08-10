from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from passive_devices.realization import (
    map_ideal_netlist_passives,
    realize_passives,
    realize_project_passives,
    solve_passive,
)
from passive_devices.mapping import (
    CallablePassiveEvaluator,
    DeviceEvaluation,
    PassiveMappingError,
    PassiveMappingConstraints,
    PassiveMappingResult,
    map_capacitor,
    map_passive,
    map_resistor,
    register_passive_evaluator,
    unregister_passive_evaluator,
)
from pdk_integration.passive_probe import render_cdf_probe
from pdk_integration.profiles import (
    PassiveDeviceProfile,
    get_pdk_profile,
    validate_pdk_profile,
)
from models import SimResult
from topologies import get_topology
from virtuoso_schematic_generation.exporter import load_device_map
from virtuoso_schematic_generation.parser import parse_netlist


class PassiveRealizationTests(unittest.TestCase):
    def test_default_tsmc28_maps_compensation_capacitor_with_cdf_callback(self):
        mapped = PassiveMappingResult(
            device_kind="capacitor",
            device_type="finger_mom_2t",
            target_value=500e-15,
            actual_value=500.1e-15,
            relative_error=0.0002,
            params={
                "w": 50e-9,
                "s": 50e-9,
                "lr": 3e-6,
                "nr": 98,
                "stm": 1,
                "spm": 6,
                "multi": 1,
            },
            evaluator_backend="virtuoso_cdf_callback",
        )
        with patch(
            "pdk_integration.cdf_evaluator.CdfCfmomTargetMapper.map_candidates",
            return_value=[mapped],
        ) as callback:
            result = map_capacitor(500e-15)

        self.assertEqual(result.device_type, "finger_mom_2t")
        self.assertEqual(result.evaluator_backend, "virtuoso_cdf_callback")
        self.assertLess(result.relative_error, 0.02)
        self.assertEqual(result.params["w"], 50e-9)
        self.assertEqual(result.params["s"], 50e-9)
        self.assertEqual(result.params["stm"], 1)
        self.assertEqual(result.params["spm"], 6)
        callback.assert_called_once()

    def test_default_tsmc28_high_res_poly_maps_to_grid_geometry(self):
        result = map_resistor(10_000.0, "high_res_poly")

        self.assertEqual(result.device_type, "high_res_poly")
        self.assertEqual(result.evaluator_backend, "analytic_profile_fallback")
        self.assertEqual(result.params["w"], 2e-6)
        grid_steps = result.params["l"] / 5e-9
        self.assertAlmostEqual(grid_steps, round(grid_steps))
        self.assertLess(result.relative_error, 0.02)

    def test_map_1k_resistor_uses_pdk_black_box_callback(self):
        profile, resistor, _ = self._callback_profile()
        calls = []

        def pdk_resistor(device, params):
            calls.append(dict(params))
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            actual = 100.0 * (length + 0.2e-6) / (width - 0.05e-6) + 25.0
            return DeviceEvaluation(
                actual,
                width * length,
                resolved_params=dict(params),
                metadata={"source": "mock_cdf"},
            )

        result = map_resistor(
            1_000.0,
            "callback_rpoly",
            profile=profile,
            evaluator=CallablePassiveEvaluator(pdk_resistor, backend_name="mock_cdf"),
        )

        self.assertLess(result.relative_error, resistor.value_tolerance)
        self.assertEqual(result.device_type, "callback_rpoly")
        self.assertEqual(result.evaluator_backend, "mock_cdf")
        self.assertGreater(len(calls), 2)
        self.assertEqual(result.to_dict()["target_R"], 1_000.0)

    def test_profile_evaluator_key_resolves_registered_pdk_callback(self):
        profile, _, _ = self._callback_profile()

        def pdk_resistor(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            return 100.0 * (length + 0.2e-6) / (width - 0.05e-6) + 25.0

        register_passive_evaluator(
            "test_rpoly_callback",
            CallablePassiveEvaluator(pdk_resistor, backend_name="registered_cdf"),
        )
        try:
            result = map_resistor(1_000.0, "callback_rpoly", profile=profile)
        finally:
            unregister_passive_evaluator("test_rpoly_callback")

        self.assertEqual(result.evaluator_backend, "registered_cdf")

    def test_unregistered_callback_and_unreachable_target_report_no_solution(self):
        profile, resistor, _ = self._callback_profile()
        with self.assertRaisesRegex(PassiveMappingError, "not registered"):
            map_resistor(1_000.0, "callback_rpoly", profile=profile)

        limited = replace(
            resistor,
            max_series_units=1,
            max_parallel_units=1,
        )
        profile = replace(
            profile,
            passive_devices={**profile.passive_devices, "callback_rpoly": limited},
        )
        evaluator = CallablePassiveEvaluator(
            lambda device, params: 100.0
            * float(params[device.length_parameter])
            / float(params[device.width_parameter])
        )
        with self.assertRaisesRegex(PassiveMappingError, "cannot realize"):
            map_resistor(
                1e9,
                "callback_rpoly",
                profile=profile,
                evaluator=evaluator,
            )

    def test_map_10k_resistor_uses_root_search_and_grid(self):
        profile, resistor, _ = self._callback_profile()

        def pdk_resistor(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            return 100.0 * (length + 0.2e-6) / (width - 0.05e-6) + 25.0

        result = map_passive(
            "resistor",
            10_000.0,
            "callback_rpoly",
            PassiveMappingConstraints(fixed_width_m=1e-6),
            profile=profile,
            evaluator=CallablePassiveEvaluator(pdk_resistor),
        )

        length = float(result.params[resistor.length_parameter])
        self.assertLess(result.relative_error, resistor.value_tolerance)
        self.assertAlmostEqual(length / resistor.geometry_grid_m, round(length / resistor.geometry_grid_m))
        self.assertEqual(result.series_units, 1)

    def test_map_1pf_capacitor_uses_pdk_black_box_callback(self):
        profile, _, capacitor = self._callback_profile()

        def pdk_capacitor(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            effective_w = width - 0.1e-6
            effective_l = length - 0.1e-6
            actual = (
                1e-3 * effective_w * effective_l
                + 20e-12 * (effective_w + effective_l)
                + 20e-15
            )
            return DeviceEvaluation(actual, width * length)

        result = map_capacitor(
            1e-12,
            "callback_mim",
            profile=profile,
            evaluator=CallablePassiveEvaluator(pdk_capacitor, backend_name="mock_pcell"),
        )

        self.assertLess(result.relative_error, capacitor.value_tolerance)
        self.assertEqual(result.parallel_units, 1)
        self.assertEqual(result.to_dict()["target_C"], 1e-12)

    def test_map_10pf_capacitor_decomposes_beyond_single_pcell_range(self):
        profile, _, capacitor = self._callback_profile()

        def pdk_capacitor(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            effective_w = width - 0.1e-6
            effective_l = length - 0.1e-6
            return (
                1e-3 * effective_w * effective_l
                + 20e-12 * (effective_w + effective_l)
                + 20e-15
            )

        result = map_capacitor(
            10e-12,
            "callback_mim",
            profile=profile,
            evaluator=CallablePassiveEvaluator(pdk_capacitor),
        )

        self.assertLess(result.relative_error, capacitor.value_tolerance)
        self.assertGreater(result.parallel_units, 1)
        self.assertLessEqual(
            float(result.params[capacitor.width_parameter]), capacitor.max_width_m
        )
        self.assertLessEqual(
            float(result.params[capacitor.length_parameter]), capacitor.max_length_m
        )

    def test_resistor_target_beyond_single_pcell_uses_series_decomposition(self):
        profile, resistor, _ = self._callback_profile()
        limited = replace(
            resistor,
            max_length_m=20e-6,
            max_series_units=8,
            value_tolerance=0.005,
        )
        profile = replace(
            profile,
            passive_devices={**profile.passive_devices, "callback_rpoly": limited},
        )

        def pdk_resistor(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            return 100.0 * (length + 0.2e-6) / (width - 0.05e-6) + 25.0

        result = map_resistor(
            10_000.0,
            "callback_rpoly",
            profile=profile,
            evaluator=CallablePassiveEvaluator(pdk_resistor),
        )

        self.assertLess(result.relative_error, limited.value_tolerance)
        self.assertGreater(result.series_units, 1)

    def test_pdk_callback_can_resolve_multiplier_parameter(self):
        profile, resistor, _ = self._callback_profile()
        multiplied = replace(
            resistor,
            min_length_m=10e-6,
            max_length_m=10e-6,
            multiplier_parameter="m",
            max_multiplier=4,
            max_series_units=1,
            max_parallel_units=1,
        )
        profile = replace(
            profile,
            passive_devices={**profile.passive_devices, "callback_rpoly": multiplied},
        )

        def pdk_resistor(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            multiplier = int(params.get(device.multiplier_parameter, 1))
            return (
                100.0 * (length + 0.2e-6) / (width - 0.05e-6) + 25.0
            ) / multiplier

        target = pdk_resistor(multiplied, {"w": 1e-6, "l": 10e-6, "m": 4})
        result = map_resistor(
            target,
            "callback_rpoly",
            PassiveMappingConstraints(fixed_width_m=1e-6),
            profile=profile,
            evaluator=CallablePassiveEvaluator(pdk_resistor),
        )

        self.assertEqual(result.params["m"], 4)
        self.assertLess(result.relative_error, multiplied.value_tolerance)

    def test_generic_ideal_netlist_recognizes_and_replaces_r_and_c(self):
        profile, _, _ = self._callback_profile()

        def evaluate(device, params):
            width = float(params[device.width_parameter])
            length = float(params[device.length_parameter])
            if device.kind == "resistor":
                return 100.0 * (length + 0.2e-6) / (width - 0.05e-6) + 25.0
            return 1e-3 * (width - 0.1e-6) * (length - 0.1e-6) + 20e-15

        netlist = """simulator lang=spectre
subckt unit (a b c)
R1 (a b) resistor r=1k
C1 (b c) capacitor c=1p
ends unit
"""
        mapped, records = map_ideal_netlist_passives(
            netlist,
            profile,
            evaluators={
                "callback_rpoly": CallablePassiveEvaluator(evaluate),
                "callback_mim": CallablePassiveEvaluator(evaluate),
            },
        )

        self.assertNotIn(" resistor r=1k", mapped)
        self.assertNotIn(" capacitor c=1p", mapped)
        self.assertIn("callback_rpoly_model", mapped)
        self.assertIn("callback_mim_model", mapped)
        self.assertEqual({record.instance for record in records}, {"R1", "C1"})

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

    def test_lookup_capacitor_preserves_multidimensional_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "cfmom.json"
            table.write_text(
                json.dumps({"version": "unit", "points": [
                    {
                        "value": 8e-12,
                        "params": {
                            "w": 50e-9, "s": 50e-9, "lr": 40e-6,
                            "nr": 288, "stm": 1, "spm": 8, "multi": 1,
                        },
                        "estimated_area_m2": 1e-9,
                    }
                ]}),
                encoding="utf-8",
            )
            device = replace(
                self._capacitor(),
                mapping_mode="lookup",
                lookup_table_path=str(table),
                width_parameter="w",
                length_parameter="lr",
                max_parallel_units=16,
                value_tolerance=0.01,
            )

            candidate = solve_passive(96e-12, device)

            self.assertEqual(candidate.parallel_units, 12)
            self.assertEqual(candidate.params["nr"], 288)
            self.assertEqual(candidate.params["spm"], 8)
            self.assertAlmostEqual(candidate.achieved_value, 96e-12)
            self.assertAlmostEqual(candidate.unit_area_m2, 1e-9)

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
            profile_data["gmid_table_path"] = "characterization/gmid.json"
            profile_data["passive_device_catalog"] = {}
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

            with patch.dict("os.environ", {"GMID_TABLE_PATH": ""}):
                profile = get_pdk_profile(str(profile_path))
                errors = validate_pdk_profile(profile)

            self.assertEqual(
                Path(profile.passive_devices["rpoly"].lookup_table_path),
                table.resolve(),
            )
            self.assertEqual(
                Path(profile.gmid_table_path),
                (root / "characterization" / "gmid.json").resolve(),
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
        with patch("pdk_integration.profiles.get_pdk_profile", return_value=profile), patch(
            "virtuoso_schematic_generation.models.get_pdk_profile", return_value=profile
        ):
            ir = parse_netlist(netlist)
            device_map = load_device_map()

        self.assertEqual([item.model for item in ir.instances], ["unit_rpoly", "unit_mim"])
        self.assertEqual(device_map["unit_rpoly"].cell, "rpoly")
        self.assertEqual(device_map["unit_mim"].cell, "mimcap")

    def test_cdf_probe_uses_configured_pcell_without_modifying_it(self):
        with patch("pdk_integration.passive_probe.get_pdk_profile", return_value=self._profile()):
            skill = render_cdf_probe("unit_mim", "report.txt")

        self.assertIn('ddGetObj("unitTech" "mimcap")', skill)
        self.assertIn("cdfGetBaseCellCDF", skill)
        self.assertIn("master~>terminals", skill)
        self.assertNotIn("dbCreateInst", skill)
        self.assertTrue(skill.rstrip().endswith("exit()"))

    def test_cdf_probe_accepts_unmapped_catalog_device(self):
        profile = replace(
            self._profile(),
            passive_device_catalog={
                "finger_mom_2t": {
                    "kind": "capacitor",
                    "virtuoso_cells": ["cfmom_2t"],
                }
            },
        )
        with patch("pdk_integration.passive_probe.get_pdk_profile", return_value=profile):
            skill = render_cdf_probe("finger_mom_2t", "report.txt")

        self.assertIn('ddGetObj("tsmcN28" "cfmom_2t")', skill)
        self.assertIn("param~>minVal", skill)
        self.assertIn("param~>maxVal", skill)
        self.assertIn("param~>callback", skill)
        self.assertIn('fprintf(out "simInfo', skill)

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

    @staticmethod
    def _callback_profile():
        resistor = PassiveDeviceProfile(
            kind="resistor",
            spectre_model="callback_rpoly_model",
            virtuoso_lib="unitTech",
            virtuoso_cell="callback_rpoly",
            mapping_mode="callback",
            evaluator_key="test_rpoly_callback",
            min_width_m=0.5e-6,
            max_width_m=5e-6,
            min_length_m=0.5e-6,
            max_length_m=100e-6,
            geometry_grid_m=0.01e-6,
            default_width_m=1e-6,
            max_aspect_ratio=120.0,
            max_series_units=4,
            max_parallel_units=2,
            value_tolerance=0.002,
            sheet_resistance_ohm_per_square=100.0,
        )
        capacitor = PassiveDeviceProfile(
            kind="capacitor",
            spectre_model="callback_mim_model",
            virtuoso_lib="unitTech",
            virtuoso_cell="callback_mim",
            mapping_mode="callback",
            evaluator_key="test_mim_callback",
            min_width_m=1e-6,
            max_width_m=50e-6,
            min_length_m=1e-6,
            max_length_m=50e-6,
            geometry_grid_m=0.1e-6,
            default_aspect_ratio=1.0,
            max_aspect_ratio=4.0,
            max_parallel_units=8,
            value_tolerance=0.005,
            capacitance_per_area_f_per_m2=1e-3,
        )
        profile = replace(
            get_pdk_profile(),
            passive_devices={
                "callback_rpoly": resistor,
                "callback_mim": capacitor,
            },
            passive_role_map={},
        )
        return profile, resistor, capacitor


if __name__ == "__main__":
    unittest.main()
