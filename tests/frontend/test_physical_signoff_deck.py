from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from analogskills.imported_design.flow import _materialize_lvs_deck
from analogskills.pdk import resolve_pdk_config


class PhysicalSignoffDeckTest(unittest.TestCase):
    def test_lvs_deck_declares_profile_streamout_text_as_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "signoff" / "lvs").mkdir(parents=True)
            template = root / "template.lvs"
            template.write_text(
                'LAYOUT PATH "old.gds"\n'
                'LAYOUT PRIMARY "old"\n'
                'SOURCE PATH "old.cdl"\n'
                'SOURCE PRIMARY "old"\n'
                'LVS REPORT "old.report"\n'
                'TEXT LAYER 626 ATTACH 626 M1\n',
                encoding="utf-8",
            )
            deck, _ = _materialize_lvs_deck(
                root,
                "ota",
                root / "ota.gds",
                root / "ota.cdl",
                template,
                resolve_pdk_config("crn28hpcp"),
            )
            text = deck.read_text(encoding="utf-8")
            for layer in range(625, 637):
                self.assertIn(f"PORT LAYER TEXT {layer}", text)


if __name__ == "__main__":
    unittest.main()
