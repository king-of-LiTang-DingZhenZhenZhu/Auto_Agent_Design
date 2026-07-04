from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from config import Settings
from main import _prepare_workspace_for_new_optimization
from models import DesignTarget
from pdk_profiles import get_pdk_profile, spectre_include_line, validate_pdk_profile
from topologies import get_topology


class OptimizerConfigTest(unittest.TestCase):
    def test_topology_escalation_is_disabled_by_default(self):
        self.assertFalse(Settings().enable_topology_escalation)

    def test_default_gmid_lookup_table_path_exists(self):
        from pathlib import Path

        self.assertTrue(Path(Settings().gmid_table_path).exists())

    def test_gmid_lookup_loads_lvt_tables(self):
        from gmid_lookup import GmidLookup

        lookup = GmidLookup(Settings().gmid_table_path)
        self.assertIn(-0.2, {round(v, 1) for v in lookup.get_available_Vbss("pch_lvt_mac")})
        result = lookup.lookup(
            "pch_lvt_mac",
            gm_id=12.0,
            L=300e-9,
            Vds=0.3,
            Vbs=-0.2,
        )
        self.assertEqual(result.model, "pch_lvt_mac")
        self.assertAlmostEqual(result.Vbs, -0.2)

    def test_gmid_lookup_table_path_can_come_from_env(self):
        with patch.dict("os.environ", {"GMID_TABLE_PATH": "/tmp/custom_gmid.json"}):
            self.assertEqual(Settings().gmid_table_path, "/tmp/custom_gmid.json")

    def test_pdk_profile_validation_checks_gmid_model_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gmid = root / "gmid.json"
            gmid.write_text(
                json.dumps({"unit_n": {}, "unit_p": {}}),
                encoding="utf-8",
            )
            profile_json = root / "unit_pdk.json"
            profile_json.write_text(
                json.dumps(
                    {
                        "name": "unit_pdk",
                        "spectre_model_path": "/fake/models.scs",
                        "spectre_section": "tt",
                        "hspice_model_path": "/fake/models.l",
                        "hspice_section": "TT",
                        "nmos_model": "unit_n",
                        "pmos_model": "unit_p",
                        "nmos_lvt_model": "missing_lvt_n",
                        "pmos_lvt_model": "missing_lvt_p",
                        "process_sections": {
                            "tt": "tt",
                            "ss": "ss",
                            "ff": "ff",
                        },
                        "vdd": 1.0,
                        "vdd_min": 0.9,
                        "vdd_max": 1.1,
                        "pvt_temperatures_c": [-40, 27, 125],
                        "min_l": 100e-9,
                        "max_width_per_finger": 1e-6,
                        "min_width_per_finger": 100e-9,
                        "gmid_table_path": str(gmid),
                        "spectre_options": ["rawfmt=psfascii"],
                        "virtuoso_tech_lib": "unitTech",
                        "virtuoso_pdk_lib_path": "/fake/unitTech",
                    }
                ),
                encoding="utf-8",
            )
            profile = get_pdk_profile(str(profile_json))
            errors = validate_pdk_profile(
                profile,
                require_gmid=True,
                required_model_roles=("nmos", "pmos"),
            )
            self.assertFalse(errors)

            errors = validate_pdk_profile(
                profile,
                require_gmid=True,
                required_model_roles=("nmos_lvt", "pmos_lvt"),
            )
            self.assertTrue(any("missing_lvt_n" in error for error in errors))

    def test_spectre_include_normalizes_common_pdk_absolute_path(self):
        with patch.dict(
            "os.environ",
            {"PDK_SPECTRE_PATH": "PDKS/TSMC28nm/models/spectre/toplevel.scs"},
        ):
            self.assertIn(
                'include "/PDKS/TSMC28nm/models/spectre/toplevel.scs"',
                spectre_include_line(),
            )

    def test_topology_generation_uses_env_pdk_profile_overrides(self):
        with patch.dict(
            "os.environ",
            {
                "PDK_SPECTRE_PATH": "/unit/pdk/spectre.scs",
                "PDK_SPECTRE_SECTION": "unit_tt",
                "NMOS_MODEL": "unit_n",
                "PMOS_MODEL": "unit_p",
                "NMOS_LVT_MODEL": "unit_n_lvt",
                "PMOS_LVT_MODEL": "unit_p_lvt",
                "VDD": "1.05",
                "PDK_MAX_WIDTH_PER_FINGER": "1e-6",
            },
        ):
            five_t = get_topology("5t_ota").generate_circuit()
            self.assertIn('include "/unit/pdk/spectre.scs" section=unit_tt', five_t)
            self.assertIn("unit_p", five_t)
            self.assertIn("unit_n", five_t)

            folded = get_topology("folded_cascode").generate_circuit()
            self.assertIn("unit_p_lvt", folded)
            self.assertIn("unit_n_lvt", folded)

    def test_workspace_cleanup_removes_stale_run_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(
                workspace_dir=tmp,
                outputs_dir=str(Path(tmp) / "outputs"),
                dry_run=True,
            )
            workspace = cfg.get_workspace_path()
            stale_run = workspace / "run_003"
            stale_run.mkdir(parents=True)
            (stale_run / "raw").mkdir()
            (stale_run / "raw" / "old.ac").write_text("stale", encoding="utf-8")
            for name in ("history.json", "optimization_metrics.csv"):
                (workspace / name).write_text("stale", encoding="utf-8")
            (workspace / "initial_gmid").mkdir()

            _prepare_workspace_for_new_optimization(cfg)

            self.assertFalse(stale_run.exists())
            self.assertFalse((workspace / "history.json").exists())
            self.assertFalse((workspace / "optimization_metrics.csv").exists())
            self.assertFalse((workspace / "initial_gmid").exists())

    def test_5t_gmid_space_derives_vbias_and_constrains_tail_current(self):
        targets = DesignTarget(
            bandwidth_hz=100e6,
            load_cap_f=1e-12,
        )
        spec = get_topology("5t_ota").get_gmid_spec(targets)
        self.assertIsNotNone(spec)
        self.assertNotIn(
            "VBIAS",
            [param.name for param in spec.pass_through_params],
        )
        self.assertEqual(spec.derived_gate_biases[0].param_name, "VBIAS")

        tail = next(
            branch for branch in spec.branch_currents if branch.name == "I_tail"
        )
        expected_min = 2.0 * 3.141592653589793 * 100e6 * 1e-12 / (24.0 * 0.5)
        self.assertAlmostEqual(tail.low, expected_min)
        self.assertGreaterEqual(tail.default, tail.low)

    def test_all_tail_devices_use_point_two_volt_vds_estimate(self):
        for name in (
            "5t_ota",
            "two_stage_ota",
            "nmcf_three_stage",
        ):
            spec = get_topology(name).get_gmid_spec()
            tail_devices = [
                transistor
                for transistor in spec.transistors
                if "tail" in transistor.role
            ]
            self.assertTrue(tail_devices)
            for transistor in tail_devices:
                self.assertEqual(transistor.Vds_estimate, 0.2)

    def test_two_stage_current_bounds_follow_gbw_cl_estimate(self):
        targets = DesignTarget(
            bandwidth_hz=100e6,
            load_cap_f=1e-12,
        )
        spec = get_topology("two_stage_ota").get_gmid_spec(targets)
        currents = {branch.name: branch for branch in spec.branch_currents}
        x = 2.0 * 3.141592653589793 * 100e6 * (0.5e-12) / 15.0
        self.assertAlmostEqual(currents["I_tail"].low, max(50e-6, 2.0 * x))
        self.assertAlmostEqual(currents["I_tail"].high, 10.0 * currents["I_tail"].low)
        self.assertNotIn("I_cs", currents)

    def test_two_stage_gmid_space_uses_integer_mirror_ratio(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        param_space = spec.build_param_space()
        params = {param.name: param for param in param_space.params}
        self.assertIn("I_tail", params)
        self.assertIn("ratio_load_tail", params)
        self.assertNotIn("I_cs", params)
        self.assertNotIn("gm_id_load_nmos", params)
        self.assertNotIn("L_load_nmos", params)
        self.assertNotIn("L_cs_pmos", params)
        ratio = params["ratio_load_tail"]
        self.assertEqual(ratio.value_type, "int")
        self.assertEqual(ratio.low, 1)
        self.assertEqual(ratio.high, 3)

    def test_current_source_and_load_lengths_are_constrained(self):
        physical_roles = {
            "5t_ota": ("Ltail", "Lcm"),
            "two_stage_ota": ("Ltail", "Lload"),
            "nmcf_three_stage": ("Ltail1", "Lload1", "Lload2", "Lload3"),
        }
        gmid_roles = {
            "5t_ota": ("tail_pmos", "mirror_nmos"),
            "two_stage_ota": ("tail_nmos", "load_nmos"),
            "nmcf_three_stage": (
                "stage1_tail_pmos",
                "stage1_load_nmos",
                "stage2_load_pmos",
                "stage3_load_nmos",
            ),
        }

        for topology_name, param_names in physical_roles.items():
            params = {
                param.name: param
                for param in get_topology(topology_name).get_param_space().params
            }
            for param_name in param_names:
                self.assertAlmostEqual(params[param_name].low, 200e-9)
                self.assertAlmostEqual(params[param_name].high, 600e-9)

        for topology_name, roles in gmid_roles.items():
            transistors = {
                transistor.role: transistor
                for transistor in get_topology(topology_name).get_gmid_spec().transistors
            }
            for role in roles:
                self.assertAlmostEqual(transistors[role].L_low, 200e-9)
                self.assertAlmostEqual(transistors[role].L_high, 600e-9)

    def test_second_stage_cs_length_is_bound_to_load_length(self):
        for topology_name in ("two_stage_ota",):
            topology = get_topology(topology_name)
            param_names = [param.name for param in topology.get_param_space().params]
            circuit = topology.generate_circuit()
            spec = topology.get_gmid_spec()
            gmid_params = {param.name for param in spec.build_param_space().params}

            self.assertNotIn("Lcs", param_names)
            self.assertNotIn("Lcs=", circuit)
            self.assertIn("Mcs", circuit)
            self.assertRegex(circuit, r"Mcs .* l=Lload\b")
            self.assertNotIn("L_cs_pmos", gmid_params)

    def test_folded_bias_ratio_param_space_replaces_current_source_sizes(self):
        topology = get_topology("folded_cascode")
        params = {param.name: param for param in topology.get_param_space().params}

        removed = {
            "Wtailp", "Ltailp",
            "Wfoldn", "Lfoldn",
            "Wcasn", "Lcasn",
            "Wmirrp", "Lmirrp",
            "Wcasp", "Lcasp",
            "Wload", "Lload",
        }
        self.assertFalse(removed & set(params))
        self.assertIn("m_half_unit", params)
        self.assertIn("m_load_ratio", params)
        self.assertNotIn("m_load_extra", params)
        self.assertEqual(params["m_half_unit"].value_type, "int")
        self.assertEqual(params["m_load_ratio"].value_type, "int")
        self.assertEqual(params["m_half_unit"].low, 2)
        self.assertEqual(params["m_half_unit"].high, 6)
        self.assertEqual(params["m_load_ratio"].low, 2)
        self.assertEqual(params["m_load_ratio"].high, 8)
        self.assertIn("Lbias", params)
        self.assertAlmostEqual(params["Lbias"].low, 300e-9)
        self.assertAlmostEqual(params["Lbias"].high, 600e-9)
        for name in (
            "Wbp_big", "Wbp_small", "Wbn_big", "Wbn_small",
        ):
            self.assertNotIn(name, params)

    def test_folded_gmid_space_uses_bias_ratio_currents(self):
        spec = get_topology("folded_cascode").get_gmid_spec()
        params = {param.name: param for param in spec.build_param_space().params}
        transistor_roles = {transistor.role for transistor in spec.transistors}

        self.assertEqual(transistor_roles, {"diff_pair_pmos", "cs_pmos"})
        self.assertFalse(spec.branch_currents)
        self.assertEqual(
            {current.name for current in spec.derived_branch_currents},
            {"I_tail", "I_fold", "I_cs"},
        )
        for name in (
            "gm_id_tail_pmos",
            "gm_id_fold_nmos",
            "gm_id_cas_nmos",
            "gm_id_mirr_pmos",
            "gm_id_casp_pmos",
            "gm_id_load_nmos",
            "L_cs_pmos",
        ):
            self.assertNotIn(name, params)
        self.assertIn("m_half_unit", params)
        self.assertIn("m_load_ratio", params)
        self.assertIn("Lbias", params)
        self.assertNotIn("m_load_extra", params)
        for name in (
            "Wbp_big", "Wbp_small", "Wbn_big", "Wbn_small",
            "Lbp_big", "Lbp_small", "Lbn_big", "Lbn_small",
        ):
            self.assertNotIn(name, params)
        for name in (
            "gm_id_bias_pmos_big",
            "gm_id_bias_pmos_small",
            "gm_id_bias_nmos_big",
            "gm_id_bias_nmos_small",
        ):
            self.assertNotIn(name, params)
        self.assertEqual(spec.fixed_params["Wbp_big"], 4.8e-6)
        self.assertEqual(spec.fixed_params["nf_Wbp_big"], 4)
        self.assertEqual(spec.fixed_params["m_Wbp_big"], 1)
        self.assertEqual(spec.fixed_params["Wbp_small"], 1.2e-6)
        self.assertEqual(spec.fixed_params["nf_Wbp_small"], 1)
        self.assertEqual(spec.fixed_params["m_Wbp_small"], 1)
        self.assertEqual(spec.fixed_params["Wbn_big"], 4.8e-6)
        self.assertEqual(spec.fixed_params["nf_Wbn_big"], 4)
        self.assertEqual(spec.fixed_params["m_Wbn_big"], 1)
        self.assertEqual(spec.fixed_params["Wbn_small"], 1.2e-6)
        self.assertEqual(spec.fixed_params["nf_Wbn_small"], 1)
        self.assertEqual(spec.fixed_params["m_Wbn_small"], 1)
        self.assertEqual(spec.fixed_width_scale_param, "Lbias")
        self.assertEqual(spec.fixed_width_scale_reference, 400e-9)

    def test_two_stage_gmid_space_derives_nmos_vbias(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        self.assertNotIn(
            "VBIAS",
            [param.name for param in spec.pass_through_params],
        )
        self.assertEqual(len(spec.derived_gate_biases), 1)
        bias = spec.derived_gate_biases[0]
        self.assertEqual(bias.role, "tail_nmos")
        self.assertEqual(bias.param_name, "VBIAS")
        self.assertEqual(bias.device_type, "nmos")
        self.assertEqual(bias.supply_voltage, 0.0)

    def test_two_stage_diff_pair_uses_negative_body_bias_for_lookup(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        transistors = {transistor.role: transistor for transistor in spec.transistors}

        self.assertEqual(transistors["diff_pair_nmos"].Vbs, -0.3)
        self.assertEqual(transistors["tail_nmos"].Vbs, 0.0)
        self.assertEqual(transistors["load_nmos"].Vbs, 0.0)

    def test_gmid_specs_use_lookup_supported_body_biases(self):
        supported_vbs_by_model = {
            "nch_mac": {-0.3, 0.0},
            "pch_mac": {-0.3, 0.0},
            "nch_lvt_mac": {-0.2, 0.0},
            "pch_lvt_mac": {-0.2, 0.0},
        }
        for name in (
            "5t_ota",
            "two_stage_ota",
            "folded_cascode",
            "nmcf_three_stage",
        ):
            spec = get_topology(name).get_gmid_spec()
            for transistor in spec.transistors:
                self.assertIn(
                    transistor.Vbs,
                    supported_vbs_by_model[transistor.model],
                    f"{name}:{transistor.role} Vbs={transistor.Vbs}",
                )

    def test_folded_cascode_uses_lvt_gmid_models(self):
        spec = get_topology("folded_cascode").get_gmid_spec()
        transistors = {transistor.role: transistor for transistor in spec.transistors}

        self.assertEqual(transistors["diff_pair_pmos"].model, "pch_lvt_mac")
        self.assertEqual(transistors["diff_pair_pmos"].Vbs, -0.2)
        self.assertEqual(transistors["cs_pmos"].model, "pch_lvt_mac")

    def test_vbias_physical_ranges_are_topology_owned(self):
        five_t_params = {
            param.name: param for param in get_topology("5t_ota").get_param_space().params
        }
        two_stage_params = {
            param.name: param
            for param in get_topology("two_stage_ota").get_param_space().params
        }

        self.assertEqual(five_t_params["VBIAS"].low, 0.15)
        self.assertEqual(five_t_params["VBIAS"].high, 0.55)
        self.assertEqual(two_stage_params["VBIAS"].low, 0.4)
        self.assertEqual(two_stage_params["VBIAS"].high, 0.85)

    def test_folded_derived_currents_follow_bias_ratios(self):
        spec = get_topology("folded_cascode").get_gmid_spec()
        currents = {
            current.name: current.resolve(
                {"m_half_unit": 3, "m_load_ratio": 5}
            )
            for current in spec.derived_branch_currents
        }

        self.assertAlmostEqual(currents["I_tail"], 120e-6)
        self.assertAlmostEqual(currents["I_fold"], 120e-6)
        self.assertAlmostEqual(currents["I_cs"], 300e-6)


if __name__ == "__main__":
    unittest.main()
