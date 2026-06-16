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
            vgs=0.42,
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
            "ratio_load_tail": 4,
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

        self.assertAlmostEqual(wload_total, 4.0 * wtail_total)
        self.assertAlmostEqual(physical["Lload"], physical["Ltail"])
        self.assertAlmostEqual(wcs_total, 4.0 * wtail_total)
        self.assertAlmostEqual(physical["VBIAS"], 0.42)


if __name__ == "__main__":
    unittest.main()
