from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from config import Settings
from main import _prepare_workspace_for_new_optimization
from models import DesignTarget
from pdk_profiles import (
    apply_topology_preset,
    get_pdk_profile,
    get_topology_preset,
    spectre_include_line,
    validate_pdk_profile,
)
from topologies import get_topology


class OptimizerConfigTest(unittest.TestCase):
    def _write_unit_profile(
        self,
        root: Path,
        topology_presets: dict | None = None,
        **overrides,
    ) -> Path:
        profile_json = root / "unit_pdk.json"
        data = {
            "name": "unit_pdk",
            "spectre_model_path": "/unit/pdk/spectre.scs",
            "spectre_section": "unit_tt",
            "hspice_model_path": "/unit/pdk/hspice.l",
            "hspice_section": "UNIT_TT",
            "nmos_model": "unit_n",
            "pmos_model": "unit_p",
            "nmos_lvt_model": "unit_n_lvt",
            "pmos_lvt_model": "unit_p_lvt",
            "process_sections": {"tt": "unit_tt", "ss": "unit_ss", "ff": "unit_ff"},
            "vdd": 1.05,
            "vdd_min": 0.9,
            "vdd_max": 1.1,
            "pvt_temperatures_c": [-40, 27, 125],
            "min_l": 100e-9,
            "max_width_per_finger": 1e-6,
            "min_width_per_finger": 100e-9,
            "gmid_table_path": str(root / "gmid.json"),
            "spectre_options": ["rawfmt=psfascii"],
            "virtuoso_tech_lib": "unitTech",
            "virtuoso_pdk_lib_path": "/unit/pdk/tech",
            "topology_presets": topology_presets or {},
        }
        data.update(overrides)
        profile_json.write_text(json.dumps(data), encoding="utf-8")
        return profile_json

    def test_topology_escalation_is_disabled_by_default(self):
        self.assertFalse(Settings().enable_topology_escalation)

    def test_bo_uses_twenty_startup_trials_by_default(self):
        self.assertEqual(Settings().bo_n_startup_trials, 20)

    def test_llm_validation_is_disabled_by_default(self):
        settings = Settings(deepseek_api_key="", dry_run=False)
        self.assertFalse(settings.enable_llm_validation)
        settings.validate_required()

    def test_llm_validation_frequency_is_ignored_when_disabled(self):
        settings = Settings(
            enable_llm_validation=False,
            llm_validation_frequency=5,
            deepseek_api_key="",
            dry_run=False,
        )
        settings.validate_required()

    def test_llm_validation_requires_api_key_when_enabled(self):
        settings = Settings(
            enable_llm_validation=True,
            llm_validation_frequency=5,
            deepseek_api_key="",
            dry_run=False,
        )
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            settings.validate_required()

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

    def test_external_pdk_profile_loads_topology_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_json = self._write_unit_profile(
                root,
                {
                    "5t_ota": {
                        "default_params": {"Wtail": 9e-6, "VBIAS": 0.42},
                        "testbench_defaults": {"VCM": 0.22, "CL": 750e-15},
                        "param_space_overrides": {
                            "VBIAS": {"low": 0.25, "high": 0.65},
                            "Wtail": {"high": 300e-6},
                        },
                    }
                },
            )

            with patch.dict("os.environ", {"PDK_PROFILE_FILE": str(profile_json)}):
                preset = get_topology_preset("5t_ota")
                self.assertEqual(preset["default_params"]["Wtail"], 9e-6)
                merged = apply_topology_preset("5t_ota", {"Wtail": 3e-6})
                self.assertEqual(merged["Wtail"], 9e-6)

                topology = get_topology("5t_ota")
                self.assertEqual(topology.get_default_params()["VBIAS"], 0.42)
                circuit = topology.generate_circuit()
                self.assertIn("parameters Wtail=9u", circuit)
                testbench = topology.generate_testbench()
                self.assertIn("parameters VDD=1.05 VCM=0.22 VBIAS=0.42", testbench)
                self.assertIn("CL=750f", testbench)

                params = {param.name: param for param in topology.get_param_space().params}
                self.assertEqual(params["VBIAS"].low, 0.25)
                self.assertEqual(params["VBIAS"].high, 0.65)
                self.assertEqual(params["Wtail"].high, 300e-6)

    def test_missing_topology_preset_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_json = self._write_unit_profile(root)

            with patch.dict("os.environ", {"PDK_PROFILE_FILE": str(profile_json)}):
                topology = get_topology("5t_ota")
                self.assertEqual(topology.get_default_params()["Wtail"], 3e-6)
                circuit = topology.generate_circuit()
                self.assertIn("parameters Wtail=3u", circuit)

    def test_two_stage_profile_preset_controls_testbench_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_json = self._write_unit_profile(
                root,
                {
                    "two_stage_ota": {
                        "testbench_defaults": {
                            "VCM": 0.62,
                            "VBIAS": 0.73,
                            "CL": 1e-12,
                        }
                    }
                },
            )

            with patch.dict("os.environ", {"PDK_PROFILE_FILE": str(profile_json)}):
                testbench = get_topology("two_stage_ota").generate_testbench()
                self.assertIn("parameters VDD=1.05 VCM=0.62 VBIAS=0.73", testbench)
                self.assertIn("CL=1p", testbench)

    def test_folded_profile_preset_reaches_physical_and_gmid_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_json = self._write_unit_profile(
                root,
                {
                    "folded_cascode": {
                        "default_params": {
                            "Lbias": 500e-9,
                            "Wbp_big": 6e-6,
                            "m_half_unit": 4,
                            "m_load_ratio": 3,
                            "bias_p_scale": 1.15,
                        },
                        "param_space_overrides": {
                            "m_half_unit": {"low": 3, "high": 5},
                            "bias_p_scale": {"low": 0.9, "high": 1.3},
                        },
                    }
                },
            )

            with patch.dict("os.environ", {"PDK_PROFILE_FILE": str(profile_json)}):
                topology = get_topology("folded_cascode")
                defaults = topology.get_default_params()
                self.assertEqual(defaults["Lbias"], 500e-9)
                self.assertEqual(defaults["m_half_unit"], 4)
                self.assertNotIn("Wbp_big", defaults)

                circuit = topology.generate_circuit()
                self.assertIn("parameters Lbias=500n", circuit)
                self.assertIn("parameters Wbp_big=6u*Lbias", circuit)
                self.assertIn("parameters m_half_unit=4 m_load_ratio=3", circuit)
                self.assertIn("parameters bias_p_scale=1.15", circuit)

                spec = topology.get_gmid_spec()
                self.assertEqual(spec.fixed_params["Wbp_big"], 6e-6)
                params = {param.name: param for param in spec.build_param_space().params}
                self.assertEqual(params["m_half_unit"].low, 3)
                self.assertEqual(params["m_half_unit"].high, 5)
                self.assertEqual(params["bias_p_scale"].low, 0.9)
                self.assertEqual(params["bias_p_scale"].high, 1.3)

    def test_topology_preset_validation_reports_unknown_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_json = self._write_unit_profile(
                root,
                {
                    "unknown_topology": {"default_params": {"Wfoo": 1e-6}},
                    "5t_ota": {
                        "default_params": {"Wdoes_not_exist": 1e-6},
                        "param_space_overrides": {"Ldoes_not_exist": {"low": 1}},
                        "testbench_defaults": {"BAD": 1.0},
                    },
                },
            )

            profile = get_pdk_profile(str(profile_json))
            errors = validate_pdk_profile(profile)
            self.assertTrue(any("unknown topology" in error for error in errors))
            self.assertTrue(any("Wdoes_not_exist" in error for error in errors))
            self.assertTrue(any("Ldoes_not_exist" in error for error in errors))
            self.assertTrue(any("BAD" in error for error in errors))

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
        expected_scale_ranges = {
            "bias_p_scale": (0.7, 1.4),
            "bias_n_scale": (0.7, 1.4),
            "bias_p_small_scale": (0.8, 1.25),
            "bias_n_small_scale": (0.8, 1.25),
        }
        for name, (low, high) in expected_scale_ranges.items():
            self.assertIn(name, params)
            self.assertEqual(params[name].low, low)
            self.assertEqual(params[name].high, high)
            self.assertFalse(params[name].log_scale)
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
        for name in (
            "bias_p_scale",
            "bias_n_scale",
            "bias_p_small_scale",
            "bias_n_small_scale",
        ):
            self.assertIn(name, params)
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
