import unittest

from monitor_hotplug import event_kind


class HotplugEventTests(unittest.TestCase):
    def test_accepts_drm_hotplug(self):
        self.assertEqual(event_kind("drm", "change /devices/pci/drm/card1 (drm)"), "drm")

    def test_ignores_non_drm_event(self):
        self.assertIsNone(event_kind("drm", "change /devices/pci/sound/card1"))

    def test_accepts_unlock_only(self):
        self.assertEqual(event_kind("lock", "ActiveChanged (false,)"), "unlock")
        self.assertIsNone(event_kind("lock", "ActiveChanged (true,)"))


if __name__ == "__main__":
    unittest.main()
