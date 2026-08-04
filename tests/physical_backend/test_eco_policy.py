from __future__ import annotations

import unittest

from analogskills.imported_design.eco import accept_eco_candidate


class EcoPolicyTest(unittest.TestCase):
    def test_accepts_strict_nonregressing_improvement(self):
        self.assertTrue(accept_eco_candidate(before_drc=3, before_lvs=1, after_drc=2, after_lvs=1, stages_ok=True))

    def test_rejects_equal_or_regressing_candidate(self):
        self.assertFalse(accept_eco_candidate(before_drc=3, before_lvs=1, after_drc=3, after_lvs=1, stages_ok=True))
        self.assertFalse(accept_eco_candidate(before_drc=3, before_lvs=0, after_drc=2, after_lvs=1, stages_ok=True))

    def test_rejects_failed_eda_stage(self):
        self.assertFalse(accept_eco_candidate(before_drc=3, before_lvs=1, after_drc=0, after_lvs=0, stages_ok=False))


if __name__ == "__main__":
    unittest.main()
