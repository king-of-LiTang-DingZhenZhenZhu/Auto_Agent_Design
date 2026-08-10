from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pdk_cdf_evaluator import (
    CdfCfmomTargetMapper,
    CdfEvaluation,
    _cfmom_target_candidates,
    evaluate_cdf_geometries,
    parse_cdf_evaluation_report,
    render_cdf_evaluation_probe,
)
from passive_mapping import PassiveMappingConstraints
from pdk_profiles import get_pdk_profile


class PdkCdfEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.device = get_pdk_profile().passive_devices["finger_mom_2t"]

    def test_probe_uses_registered_cdf_callbacks_without_foundry_function_name(self):
        skill = render_cdf_evaluation_probe(
            device=self.device,
            requests=[
                {
                    "nr": 98,
                    "lr": 3.34e-6,
                    "w": 50e-9,
                    "s": 50e-9,
                    "stm": 1,
                    "spm": 6,
                    "multi": 1,
                }
            ],
            report_path="/share/tmp/report.tsv",
            scratch_lib="scratch",
            scratch_lib_path="/share/tmp/scratch",
        )

        self.assertIn("cdfGetInstCDF", skill)
        self.assertIn("cdfFindParamByName", skill)
        self.assertIn("evalstring(callback)", skill)
        self.assertIn('list("StopMn" "6")', skill)
        self.assertNotIn("tsmcN28_cfmom_2t_CB", skill)

    def test_report_parser_preserves_resolved_geometry(self):
        with tempfile.TemporaryDirectory(dir="/share/tmp") as tmp:
            report = Path(tmp) / "report.tsv"
            report.write_text(
                "index\tvalue\tnr\tlr\tw\ts\tstm\tspm\tmulti\n"
                "0\t181.96f\t98\t3.34u\t50n\t50n\t1\t6\t1\n",
                encoding="utf-8",
            )

            parsed = parse_cdf_evaluation_report(report, self.device)

        self.assertAlmostEqual(parsed[0].actual_value, 181.96e-15)
        self.assertEqual(parsed[0].resolved_params["nr"], 98)
        self.assertEqual(parsed[0].resolved_params["spm"], 6)
        self.assertAlmostEqual(parsed[0].resolved_params["lr"], 3.34e-6)

    def test_target_candidates_snap_length_and_use_resolved_finger_count(self):
        requests = [{"nr": 24}]
        calibration = [CdfEvaluation(actual_value=10e-15, resolved_params={"nr": 24})]

        candidates = _cfmom_target_candidates(100e-15, requests, calibration)

        self.assertTrue(candidates)
        self.assertTrue(all(item["nr"] == 24 for item in candidates))
        self.assertTrue(
            all(
                abs(item["lr"] / 10e-9 - round(item["lr"] / 10e-9)) < 1e-9
                for item in candidates
            )
        )

    def test_evaluator_rejects_incomplete_callback_report(self):
        with tempfile.TemporaryDirectory(dir="/share/tmp") as tmp:
            root = Path(tmp)
            fake_virtuoso = root / "fake_virtuoso"
            fake_virtuoso.write_text(
                "#!/bin/sh\n"
                "printf 'index\tvalue\tnr\tlr\tw\ts\tstm\tspm\tmulti\n' "
                "> cdf_evaluation.tsv\n"
                "printf '0\t10f\t6\t1u\t50n\t50n\t1\t6\t1\n' "
                ">> cdf_evaluation.tsv\n",
                encoding="ascii",
            )
            fake_virtuoso.chmod(0o755)
            request = {
                "nr": 6,
                "lr": 1e-6,
                "w": 50e-9,
                "s": 50e-9,
                "stm": 1,
                "spm": 6,
                "multi": 1,
            }

            with self.assertRaisesRegex(RuntimeError, "1 results for 2 requests"):
                evaluate_cdf_geometries(
                    self.device,
                    [request, request],
                    work_dir=root / "work",
                    virtuoso_bin=str(fake_virtuoso),
                )

    def test_target_mapper_returns_callback_resolved_geometry(self):
        profile = get_pdk_profile()
        device = profile.passive_devices["finger_mom_2t"]
        calibration_request = {
            **device.fixed_parameters,
            "nr": 6,
            "lr": 1e-6,
        }
        calibration = CdfEvaluation(
            actual_value=10e-15,
            resolved_params=dict(calibration_request),
        )

        def evaluate_candidates(_device, requests):
            return [
                CdfEvaluation(
                    actual_value=10e-15 * float(request["lr"]) / 1e-6,
                    resolved_params={**request, "spm": 6},
                )
                for request in requests
            ]

        with tempfile.TemporaryDirectory(dir="/share/tmp") as tmp:
            mapper = CdfCfmomTargetMapper(
                profile=profile,
                device_name="finger_mom_2t",
                work_dir=tmp,
            )
            with patch.object(
                mapper,
                "_calibration",
                return_value=([calibration_request], [calibration]),
            ), patch.object(mapper, "_evaluate", side_effect=evaluate_candidates):
                results = mapper.map_candidates(
                    "finger_mom_2t",
                    device,
                    100e-15,
                    PassiveMappingConstraints(),
                )

        self.assertAlmostEqual(results[0].actual_value, 100e-15)
        self.assertEqual(results[0].params["spm"], 6)
        self.assertEqual(results[0].evaluator_backend, "virtuoso_cdf_callback")
        self.assertTrue(results[0].evaluator_metadata["callback_resolved"])
        self.assertGreater(results[0].unit_area_m2, 0.0)


if __name__ == "__main__":
    unittest.main()
