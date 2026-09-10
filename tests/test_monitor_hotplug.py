import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from monitor_hotplug import connector_signature, display_suppressed, event_kind, should_run_layout


class HotplugEventTests(unittest.TestCase):
    def test_accepts_drm_hotplug(self):
        self.assertEqual(event_kind("drm", "change /devices/pci/drm/card1 (drm)"), "drm")

    def test_ignores_non_drm_event(self):
        self.assertIsNone(event_kind("drm", "change /devices/pci/sound/card1"))

    def test_accepts_unlock_only(self):
        self.assertEqual(event_kind("lock", "ActiveChanged (false,)"), "unlock")
        self.assertIsNone(event_kind("lock", "ActiveChanged (true,)"))

    def test_repeated_drm_event_does_not_run_layout(self):
        signature = (("card1-HDMI-A-1", "connected"),)
        self.assertFalse(should_run_layout("drm", signature, signature, False))

    def test_connector_change_and_unlock_run_layout(self):
        old = (("card1-HDMI-A-1", "disconnected"),)
        new = (("card1-HDMI-A-1", "connected"),)
        self.assertTrue(should_run_layout("drm", old, new, False))
        self.assertTrue(should_run_layout("unlock", new, new, False))

    def test_cooldown_blocks_all_layout_checks(self):
        self.assertFalse(should_run_layout("unlock", (), (), True))
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "display-cooldown.json").write_text(
                json.dumps({"until_epoch": 200}), encoding="utf-8")
            self.assertTrue(display_suppressed(100, root))
            self.assertFalse(display_suppressed(201, root))

    def test_reads_connector_signature(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            connector = root / "card1-HDMI-A-1"
            connector.mkdir()
            (connector / "status").write_text("connected\n", encoding="utf-8")
            self.assertEqual(connector_signature(root), (("card1-HDMI-A-1", "connected"),))


if __name__ == "__main__":
    unittest.main()
