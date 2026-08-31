import unittest
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import system_tool


class ParsingTests(unittest.TestCase):
    def test_meminfo(self):
        data = system_tool.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 25 kB\n")
        self.assertEqual(data["MemTotal"], 102400)
        self.assertEqual(data["MemAvailable"], 25600)

    def test_psi(self):
        data = system_tool.parse_psi("some avg10=1.25 avg60=0.50 total=3\nfull avg10=0.10 total=1\n")
        self.assertEqual(data["some_avg10"], 1.25)
        self.assertEqual(data["full_avg10"], 0.10)

    def test_proc_stat_separates_idle_and_iowait(self):
        total, idle, iowait = system_tool.parse_proc_stat("cpu  10 2 3 40 5 1 1 0 0 0\n")
        self.assertEqual(total, 62)
        self.assertEqual(idle, 40)
        self.assertEqual(iowait, 5)

    def test_grouping(self):
        self.assertEqual(system_tool.process_group("rviz2 -d view.rviz", "rviz2", ""), "ROS/RViz")
        self.assertEqual(system_tool.process_group("/opt/google/chrome/chrome", "chrome", ""), "Chrome")

    def test_diagnose_memory_pressure(self):
        snapshot = {
            "memory": {"available": 100, "total": 1000, "swap_out_per_sec": 30 * 1024 * 1024},
            "memory_psi": {"some_avg10": 8}, "io_psi": {"full_avg10": 0},
            "cpu_psi": {"some_avg10": 0}, "cpu_percent": 10, "top_cpu": [], "gpu": {}, "alerts": [],
            "blocked": [], "iowait_percent": 0,
        }
        self.assertEqual(system_tool.diagnose(snapshot)[0]["title"], "内存换页")

    def test_diagnose_swap_read_with_blocked_process(self):
        snapshot = {
            "memory": {"available": 500, "total": 1000, "swap_out_per_sec": 0,
                       "swap_in_per_sec": 8 * 1024 * 1024},
            "memory_psi": {"some_avg10": 0}, "io_psi": {"full_avg10": 3},
            "cpu_psi": {"some_avg10": 0}, "cpu_percent": 10, "top_cpu": [], "gpu": {}, "alerts": [],
            "blocked": [{"command": "/usr/bin/gnome-terminal-server"}], "iowait_percent": 12,
        }
        self.assertEqual(system_tool.diagnose(snapshot)[0]["title"], "Swap回读导致卡顿")

    def test_cleanup_only_inside_allowed_cache(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            cache = home / ".cache/demo"
            cache.mkdir(parents=True)
            payload = cache / "cache.bin"
            payload.write_bytes(b"1234")
            target = system_tool.CleanupTarget("test", "test", [cache])
            with mock.patch.object(system_tool, "user_home", return_value=home):
                freed, removed, errors = system_tool.clean_target(target)
            self.assertEqual((freed, removed, errors), (4, 1, []))
            self.assertFalse(payload.exists())

    def test_deep_clean_yes_never_auto_closes_user_apps(self):
        fake_app = system_tool.AppCleanupTarget("Chrome", "test", ("chrome",))
        with mock.patch.object(system_tool, "cache_targets", return_value=[]), \
             mock.patch.object(system_tool, "find_safe_helpers", return_value=[]), \
             mock.patch.object(system_tool, "app_cleanup_targets", return_value=[fake_app]), \
             mock.patch.object(system_tool, "scan_app_target", return_value=([123], 100, 200)), \
             mock.patch.object(system_tool, "stop_app_targets") as stopper, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            system_tool.run_cleanup(True, assume_yes=True)
        stopper.assert_not_called()

    def test_replay_cleanup_does_not_match_generic_rviz(self):
        replay = next(item for item in system_tool.app_cleanup_targets() if item.name == "回放/RViz")
        self.assertNotIn("rviz2 -d", replay.markers)
        self.assertTrue(any("progress_player" in marker for marker in replay.markers))


if __name__ == "__main__":
    unittest.main()
