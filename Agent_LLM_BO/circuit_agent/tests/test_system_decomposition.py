from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from models import DesignTarget
from system_decomposition import (
    SystemDecompositionError,
    SystemDesignRequest,
    SystemDesignSpec,
    decompose_system,
    write_system_project,
)
from topologies.base import ExecutableChildSpec, HierarchicalBlockSpec
from topologies.regulators.capless_ldo import default_ldo_targets


class SystemDecompositionTests(unittest.TestCase):
    def test_old_child_spec_name_remains_compatible(self):
        self.assertIs(HierarchicalBlockSpec, ExecutableChildSpec)

    def _request(self, **custom_specs) -> SystemDesignRequest:
        return SystemDesignRequest(
            system_type="bandgap",
            original_requirement="1.2 V low-power bandgap",
            targets=DesignTarget(
                vref_v=1.2,
                tempco_ppm_per_c=20,
                psrr_db=50,
                line_regulation_v_per_v=1e-3,
                startup_time_s=5e-6,
                power_w=200e-6,
                load_cap_f=2e-12,
                custom_specs=custom_specs,
            ),
        )

    def test_bandgap_decomposition_builds_valid_block_graph(self):
        spec = decompose_system(self._request())

        self.assertEqual(spec.architecture, "opamp_assisted_pnp_bandgap")
        self.assertEqual(spec.parent_topology, "bandgap_ptat")
        self.assertEqual(
            [block.block_id for block in spec.blocks],
            ["core", "bias", "startup", "opamp"],
        )
        self.assertEqual(len(spec.child_blocks()), 1)
        opamp = spec.child_blocks()[0]
        self.assertEqual(opamp.selected_topology, "two_stage_ota")
        self.assertEqual(opamp.targets.gain_db, 70.0)
        self.assertEqual(opamp.targets.power_w, 100e-6)
        self.assertEqual(opamp.pvt_targets.gain_db, 60.0)
        self.assertEqual(opamp.pvt_targets.bandwidth_hz, 5e6)
        self.assertEqual(opamp.operating_conditions["load_cap_f"], 2e-12)
        self.assertEqual(spec.unresolved_requirements, ())

    def test_bandgap_child_overrides_are_traced(self):
        spec = decompose_system(
            self._request(
                opamp_gain_db=78,
                opamp_pvt_gain_db=68,
                opamp_gbw_hz=20e6,
                opamp_pvt_gbw_hz=8e6,
                opamp_power_w=40e-6,
            )
        )
        opamp = spec.child_blocks()[0]

        self.assertEqual(opamp.targets.gain_db, 78.0)
        self.assertEqual(opamp.pvt_targets.gain_db, 68.0)
        self.assertEqual(opamp.targets.bandwidth_hz, 20e6)
        self.assertEqual(opamp.pvt_targets.bandwidth_hz, 8e6)
        self.assertEqual(opamp.budget["power_w"], 40e-6)
        derivations = {item.metric: item for item in opamp.derivations}
        self.assertEqual(derivations["gain_db"].margin, "10 dB nominal margin")

    def test_system_design_round_trip_preserves_targets(self):
        original = decompose_system(self._request(opamp_gain_db=75))
        restored = SystemDesignSpec.from_dict(original.to_dict())

        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.child_blocks()[0].targets.gain_db, 75.0)

    def test_unknown_system_type_is_rejected(self):
        with self.assertRaisesRegex(SystemDecompositionError, "Unsupported"):
            decompose_system(
                SystemDesignRequest(
                    system_type="pipeline adc",
                    targets=DesignTarget(),
                )
            )

    def test_sar_adc_decomposition_reproduces_paper_architecture(self):
        request = SystemDesignRequest(
            system_type="SAR ADC",
            original_requirement="paper-inspired low-power SAR ADC",
            voltage_domain="mixed_signal_2p5",
            constraints={"reference_source": "external 2.5 V low-noise reference"},
            targets=DesignTarget(
                power_w=1.2e-3,
                custom_specs={
                    "resolution_bits": 12,
                    "sample_rate_hz": 500e3,
                    "reference_voltage_v": 2.5,
                },
            ),
        )

        spec = decompose_system(request)

        self.assertEqual(spec.system_type, "sar_adc")
        self.assertEqual(
            spec.architecture,
            "single_ended_segmented_charge_redistribution",
        )
        self.assertEqual(spec.parent_topology, "sar_adc_segmented_cdac")
        self.assertEqual(
            [block.block_id for block in spec.blocks],
            [
                "sampling_switch",
                "cdac",
                "comparator",
                "reference_buffer",
                "sar_logic",
                "clock_powerdown",
            ],
        )
        cdac = next(block for block in spec.blocks if block.block_id == "cdac")
        self.assertEqual(cdac.operating_conditions["segmentation_bits"], [6, 6])
        self.assertEqual(cdac.operating_conditions["unit_cap_count"], 128)
        self.assertEqual(cdac.operating_conditions["unit_cap_f"], 100e-15)
        comparator = next(
            block for block in spec.blocks if block.block_id == "comparator"
        )
        self.assertIsNone(comparator.selected_topology)
        self.assertEqual(
            comparator.targets.custom_specs["input_resolution_v"],
            0.2e-3,
        )
        self.assertEqual(
            comparator.targets.custom_specs["offset_correction_range_v"],
            10e-3,
        )
        self.assertEqual(spec.unresolved_requirements[:3], (
            "sar_adc_segmented_cdac parent topology is not implemented",
            "offset-calibrated multistage comparator topology is not implemented",
            "ADC static/dynamic code-domain testbenches and metric parsers are not implemented",
        ))
        self.assertEqual(
            SystemDesignSpec.from_dict(spec.to_dict()).to_dict(),
            spec.to_dict(),
        )

    def test_sar_adc_rejects_invalid_segment_split(self):
        with self.assertRaisesRegex(
            SystemDecompositionError,
            "high_segment_bits.*low_segment_bits",
        ):
            decompose_system(
                SystemDesignRequest(
                    system_type="sar-adc",
                    targets=DesignTarget(
                        custom_specs={
                            "resolution_bits": 12,
                            "high_segment_bits": 7,
                            "low_segment_bits": 6,
                        }
                    ),
                )
            )

    def test_capless_ldo_decomposition_builds_error_amp_child(self):
        request = SystemDesignRequest(
            system_type="cap-less LDO",
            targets=default_ldo_targets(),
            voltage_domain="io_1p8",
        )

        spec = decompose_system(request)

        self.assertEqual(spec.parent_topology, "capless_ldo")
        self.assertEqual(spec.architecture, "pmos_pass_capless_ldo")
        self.assertEqual(len(spec.child_blocks()), 1)
        error_amp = spec.child_blocks()[0]
        self.assertEqual(error_amp.block_id, "error_amp")
        self.assertEqual(error_amp.selected_topology, "two_stage_ota")
        self.assertEqual(error_amp.targets.gain_db, 70.0)
        self.assertEqual(error_amp.targets.bandwidth_hz, 10e6)
        self.assertEqual(error_amp.pvt_targets.phase_margin_deg, 60.0)
        self.assertEqual(spec.unresolved_requirements, ())

    def test_write_system_project_materializes_execution_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, spec = write_system_project(
                self._request(opamp_gain_db=76),
                Path(tmp) / "bandgap_system",
            )
            system_design = json.loads(
                (project / "system_design.json").read_text(encoding="utf-8")
            )
            hierarchy = json.loads(
                (project / "hierarchy.json").read_text(encoding="utf-8")
            )
            requirements = json.loads(
                (project / "requirements.json").read_text(encoding="utf-8")
            )

            self.assertEqual(system_design, spec.to_dict())
            self.assertEqual(hierarchy["blocks"][0]["targets"]["gain_db"], 76.0)
            self.assertEqual(
                hierarchy["blocks"][0]["pvt_targets"]["gain_db"],
                60.0,
            )
            self.assertEqual(requirements["system_type"], "bandgap")
            self.assertEqual(
                requirements["system_architecture"],
                "opamp_assisted_pnp_bandgap",
            )
            self.assertEqual(requirements["system_design"], "system_design.json")

    def test_request_can_load_generated_requirements_shape(self):
        request = self._request(opamp_gain_db=74)
        data = request.targets.to_requirements_dict(request.original_requirement)
        data["system_type"] = "bandgap"

        restored = SystemDesignRequest.from_dict(data)

        self.assertEqual(restored.targets.vref_v, 1.2)
        self.assertEqual(restored.targets.custom_specs["opamp_gain_db"], 74)


if __name__ == "__main__":
    unittest.main()
