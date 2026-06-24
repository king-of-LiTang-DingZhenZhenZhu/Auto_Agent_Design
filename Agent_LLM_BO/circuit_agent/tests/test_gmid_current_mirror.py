from __future__ import annotations

import unittest

from gmid_lookup import GmidResult, GmidSizer
from topologies import get_topology


class _FakeLookup:
    def get_W(self, model, Id_target, gm_id, L, Vds=0.45, Vbs=0.0):
        return Id_target * 0.1

    def lookup(self, model, gm_id, L, Vds=0.45, Vbs=0.0):
        return GmidResult(
            model=model,
            gm_id=gm_id,
            id_w=10.0,
            ft=1e9,
            gain=20.0,
            gds=1e-6,
            cgg=1e-12,
            vgs=0.55,
            vth=0.3,
            L=L,
            Vds=Vds,
            Vbs=Vbs,
        )


class GmidCurrentMirrorTest(unittest.TestCase):
    def test_two_stage_load_uses_integer_ratio_to_tail_total_width(self):
        spec = get_topology("two_stage_ota").get_gmid_spec()
        params = {
            "I_tail": 50e-6,
            "ratio_load_tail": 2,
            "gm_id_tail_nmos": 8,
            "L_tail_nmos": 200e-9,
            "gm_id_diff_pair_nmos": 14,
            "L_diff_pair_nmos": 120e-9,
            "gm_id_mirror_pmos": 12,
            "L_mirror_pmos": 120e-9,
            "gm_id_cs_pmos": 12,
            "L_cs_pmos": 120e-9,
        }

        physical = GmidSizer(spec, _FakeLookup()).size(params)
        wtail_total = physical["Wtail"] * physical["nf_Wtail"]
        wload_total = physical["Wload"] * physical["nf_Wload"]
        wcs_total = physical["Wcs"] * physical["nf_Wcs"]

        self.assertAlmostEqual(wload_total, 2.0 * wtail_total)
        self.assertAlmostEqual(physical["Lload"], physical["Ltail"])
        self.assertAlmostEqual(wcs_total, 2.0 * wtail_total)
        self.assertAlmostEqual(physical["VBIAS"], 0.55)

    def test_folded_cascode_uses_bias_ratio_derived_currents(self):
        spec = get_topology("folded_cascode").get_gmid_spec()
        params = {
            "m_half_unit": 3,
            "m_load_extra": 5,
            "gm_id_diff_pair_pmos": 14,
            "L_diff_pair_pmos": 120e-9,
            "gm_id_cs_pmos": 12,
            # Attempts to override fixed bias params should be ignored.
            "Wbp_big": 99e-6,
            "Lbp_big": 900e-9,
            "Wbn_big": 99e-6,
            "Lbn_big": 900e-9,
            "Cc": 1e-12,
            "Rz": 1e3,
        }

        physical = GmidSizer(spec, _FakeLookup()).size(params)
        wdiff_total = (
            physical["Wdiffp"]
            * physical["nf_Wdiffp"]
            * physical["m_Wdiffp"]
        )
        wcs_total = physical["Wcs"] * physical["nf_Wcs"] * physical["m_Wcs"]

        # I_tail = 20uA * 2 * 3, diff pair side current = I_tail/2 = 60uA.
        self.assertAlmostEqual(wdiff_total, 6e-6)
        # I_cs = 20uA * (2 * 3 + 5) = 220uA.
        self.assertAlmostEqual(wcs_total, 22e-6)
        self.assertAlmostEqual(
            physical["Wbp_big"] * physical["nf_Wbp_big"] * physical["m_Wbp_big"],
            9.6e-6,
        )
        self.assertAlmostEqual(
            physical["Wbp_small"] * physical["nf_Wbp_small"] * physical["m_Wbp_small"],
            1.6e-6,
        )
        self.assertAlmostEqual(
            physical["Wbn_big"] * physical["nf_Wbn_big"] * physical["m_Wbn_big"],
            4.8e-6,
        )
        self.assertAlmostEqual(
            physical["Wbn_small"] * physical["nf_Wbn_small"] * physical["m_Wbn_small"],
            0.8e-6,
        )
        self.assertAlmostEqual(physical["Lbn_big"], 400e-9)
        self.assertEqual(physical["m_half_unit"], 3)
        self.assertEqual(physical["m_load_extra"], 5)


if __name__ == "__main__":
    unittest.main()
