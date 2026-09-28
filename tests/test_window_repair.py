import unittest

from window_repair import (
    Rect, coordinate_scale, has_useful_overlap, parse_windows, parse_workarea, safe_position,
)


class WindowRepairTests(unittest.TestCase):
    def test_reads_active_workspace_workarea(self):
        output = "0  * DG: 4480x1600  VP: 0,0  WA: 0,27 4410x1573  Workspace 1\n"
        self.assertEqual(parse_workarea(output), Rect(0, 27, 4410, 1573))

    def test_parses_window_with_title(self):
        output = "0x01  0 5204 400 900 700 org.gnome.Nautilus host Lecture notes\n"
        window = parse_windows(output)[0]
        self.assertEqual(window.rect, Rect(5204, 400, 900, 700))
        self.assertEqual(window.title, "Lecture notes")

    def test_only_repairs_windows_without_reachable_patch(self):
        workarea = Rect(0, 27, 4410, 1573)
        self.assertFalse(has_useful_overlap(Rect(5204, 400, 900, 700), workarea))
        self.assertTrue(has_useful_overlap(Rect(4330, 400, 200, 700), workarea))

    def test_safe_position_stays_inside_workarea(self):
        workarea = Rect(0, 27, 4410, 1573)
        x, y = safe_position(Rect(6184, 1700, 1000, 800), workarea)
        self.assertGreaterEqual(x, workarea.x)
        self.assertGreaterEqual(y, workarea.y)
        self.assertLessEqual(x + 1000, workarea.x + workarea.width)
        self.assertLessEqual(y + 800, workarea.y + workarea.height)

    def test_infers_mutter_move_scale_from_desktop_markers(self):
        windows = parse_windows(
            "0x1 -1 0 320 2560 1440 gjs.Gjs host @!0,160;BDHF\n"
            "0x2 -1 5120 0 1920 1200 gjs.Gjs host @!2560,0;BDHF\n"
        )
        self.assertEqual(coordinate_scale(windows), 2.0)

    def test_defaults_to_unscaled_coordinates_without_marker(self):
        self.assertEqual(coordinate_scale([]), 1.0)


if __name__ == "__main__":
    unittest.main()
