import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from monitor_hotplug import (
    connector_signature, display_suppressed, event_delay, event_kind, should_run_layout,
)


class HotplugEventTests(unittest.TestCase):
    def test_accepts_drm_hotplug(self):
        self.assertEqual(event_kind("drm", "change /devices/pci/drm/card1 (drm)"), "drm")

    def test_ignores_non_drm_event(self):
        self.assertIsNone(event_kind("drm", "change /devices/pci/sound/card1"))

    def test_tracks_lock_and_unlock_without_running_layout_directly(self):
        self.assertEqual(event_kind("lock", "ActiveChanged (false,)"), "unlock")
        self.assertEqual(event_kind("lock", "ActiveChanged (true,)"), "lock")
        self.assertFalse(should_run_layout("unlock", (), (), False))

    def test_accepts_randr_change_with_longer_settle_delay(self):
        self.assertEqual(event_kind("randr", "RRScreenChangeNotify event"), "randr")
        self.assertTrue(should_run_layout("randr", (), (), False))
        self.assertEqual(event_delay("randr"), 10.0)
        self.assertEqual(event_delay("drm"), 2.0)

    def test_repeated_drm_event_does_not_run_layout(self):
        signature = (("card1-HDMI-A-1", "connected"),)
        self.assertFalse(should_run_layout("drm", signature, signature, False))

    def test_startup_and_connector_change_run_layout(self):
        old = (("card1-HDMI-A-1", "disconnected"),)
        new = (("card1-HDMI-A-1", "connected"),)
        self.assertTrue(should_run_layout("drm", old, new, False))
        self.assertFalse(should_run_layout("unlock", new, new, False))
        self.assertTrue(should_run_layout("startup", new, new, False))

    def test_cooldown_blocks_all_layout_checks(self):
        self.assertFalse(should_run_layout("unlock", (), (), True))
        self.assertFalse(should_run_layout("startup", (), (), True))
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
