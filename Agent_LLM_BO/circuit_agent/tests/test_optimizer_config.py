from __future__ import annotations

import unittest

from config import Settings


class OptimizerConfigTest(unittest.TestCase):
    def test_topology_escalation_is_disabled_by_default(self):
        self.assertFalse(Settings().enable_topology_escalation)


if __name__ == "__main__":
    unittest.main()
