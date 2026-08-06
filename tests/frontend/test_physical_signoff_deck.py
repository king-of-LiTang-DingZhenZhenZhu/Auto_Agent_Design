from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from analogskills.imported_design.flow import _materialize_lvs_deck, _preflight
from analogskills.pdk import resolve_pdk_config


class PhysicalSignoffDeckTest(unittest.TestCase):
    def _materialize(self, root: Path, template_text: str) -> str:
        template = root / "template.lvs"
        template.write_text(template_text, encoding="utf-8")
        deck, _ = _materialize_lvs_deck(
            root,
            "ota",
            root / "ota.gds",
            root / "ota.cdl",
            template,
            resolve_pdk_config("crn28hpcp"),
        )
        return deck.read_text(encoding="utf-8")

    def test_lvs_deck_declares_profile_streamout_text_as_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "signoff" / "lvs").mkdir(parents=True)
            text = self._materialize(
                root,
                'LAYOUT PATH "old.gds"\n'
                'LAYOUT PRIMARY "old"\n'
                'SOURCE PATH "old.cdl"\n'
                'SOURCE PRIMARY "old"\n'
                'LVS REPORT "old.report"\n'
                'TEXT LAYER 626 ATTACH 626 M1\n',
            )
            for layer in range(625, 637):
                self.assertIn(f"PORT LAYER TEXT {layer}", text)

    def test_lvs_deck_wraps_generated_svrf_in_tvf_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "signoff" / "lvs").mkdir(parents=True)
            text = self._materialize(
                root,
                '#!tvf\n'
                'tvf::VERBATIM {\n'
                'LAYOUT PATH "old.gds"\n'
                'LAYOUT PRIMARY "old"\n'
                'SOURCE PATH "old.cdl"\n'
                'SOURCE PRIMARY "old"\n'
                'LVS REPORT "old.report"\n'
                '}\n',
            )
            generated = text[text.index("// Generated top-level port text layers") :]
            self.assertIn("tvf::VERBATIM {", text)
            self.assertTrue(generated.rstrip().endswith("}"))

    def test_preflight_registers_cadence_basic_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = root / "tool"
            deck = root / "deck"
            pdk_lib = root / "pdk"
            tool.write_text("", encoding="utf-8")
            deck.write_text("", encoding="utf-8")
            pdk_lib.mkdir()
            env = {
                "ANALOGSKILLS_VIRTUOSO_BINARY": str(tool),
                "ANALOGSKILLS_STRMOUT_BINARY": str(tool),
                "ANALOGSKILLS_CALIBRE_BINARY": str(tool),
                "ANALOGSKILLS_CRN28HPCP_DRC_DECK": str(deck),
                "ANALOGSKILLS_CRN28HPCP_LVS_DECK": str(deck),
                "ANALOGSKILLS_VIRTUOSO_PDK_LIB_PATH": str(pdk_lib),
            }
            with patch.dict(os.environ, env, clear=False):
                _preflight(root, "BO_Designs")
            cds_lib = (root / "cds.lib").read_text(encoding="utf-8")
            self.assertIn("DEFINE basic $CDSHOME/tools/dfII/etc/cdslib/basic", cds_lib)
            self.assertIn("DEFINE analogLib $CDSHOME/tools/dfII/etc/cdslib/artist/analogLib", cds_lib)


if __name__ == "__main__":
    unittest.main()
