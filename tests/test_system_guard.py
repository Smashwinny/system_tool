import signal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import system_guard
from incident_store import IncidentStore
from system_tool import ProcessRow


class GuardPolicyTests(unittest.TestCase):
    def snapshot(self, available, total=16 * system_guard.GIB, some=0, full=0, swap_out=0):
        return {
            "memory": {"available": available, "total": total, "swap_out_per_sec": swap_out},
            "memory_psi": {"some_avg10": some, "full_avg10": full},
        }

    def test_low_memory_without_pressure_does_not_kill(self):
        critical, _ = system_guard.memory_is_critical(
            self.snapshot(500 * system_guard.MIB), system_guard.GuardConfig())
        self.assertFalse(critical)

    def test_low_memory_with_sustained_signal_is_critical(self):
        critical, detail = system_guard.memory_is_critical(
            self.snapshot(500 * system_guard.MIB, some=25), system_guard.GuardConfig())
        self.assertTrue(critical)
        self.assertIn("PSI", detail)

    def test_pressure_with_healthy_available_memory_does_not_kill(self):
        critical, _ = system_guard.memory_is_critical(
            self.snapshot(6 * system_guard.GIB, some=50), system_guard.GuardConfig())
        self.assertFalse(critical)

    def test_selects_largest_user_group_and_protects_desktop(self):
        rows = [
            ProcessRow(10, "Chrome", "chrome", 0, 2200 * system_guard.MIB, 900 * system_guard.MIB, "S"),
            ProcessRow(11, "Desktop", "gnome-shell", 0, 4 * system_guard.GIB, 0, "S"),
            ProcessRow(12, "Build/Compiler", "cc1plus", 0, 2100 * system_guard.MIB, 0, "R"),
        ]
        with mock.patch.object(system_guard, "process_uid", return_value=1000):
            selected = system_guard.select_offender(rows, 2 * system_guard.GIB, uid=1000)
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], "Chrome")

    def test_historical_swap_does_not_choose_offender(self):
        rows = [
            ProcessRow(10, "Chrome", "chrome", 0, 500 * system_guard.MIB, 8 * system_guard.GIB, "S"),
            ProcessRow(11, "Build/Compiler", "cc1plus", 0, 3 * system_guard.GIB, 0, "R"),
        ]
        with mock.patch.object(system_guard, "process_uid", return_value=1000):
            selected = system_guard.select_offender(rows, 2 * system_guard.GIB, uid=1000)
        self.assertEqual(selected[0], "Build/Compiler")

    def test_does_not_select_small_process_group(self):
        rows = [ProcessRow(10, "Chrome", "chrome", 0, 500 * system_guard.MIB, 0, "S")]
        with mock.patch.object(system_guard, "process_uid", return_value=1000):
            self.assertIsNone(system_guard.select_offender(rows, 2 * system_guard.GIB, uid=1000))

    def test_unknown_programs_are_not_aggregated_by_comm(self):
        rows = [
            ProcessRow(10, "python3", "python3 task-a.py", 0, 1200 * system_guard.MIB, 0, "S"),
            ProcessRow(11, "python3", "python3 task-b.py", 0, 1200 * system_guard.MIB, 0, "S"),
        ]
        with mock.patch.object(system_guard, "process_uid", return_value=1000):
            selected = system_guard.select_offender(rows, 2 * system_guard.GIB, uid=1000)
        self.assertIsNone(selected)

    def test_signal_targets_only_current_user(self):
        sent = []
        with mock.patch.object(system_guard, "process_uid", side_effect=lambda pid: 1000 if pid == 10 else 1001), \
             mock.patch.object(system_guard.os, "getuid", return_value=1000):
            result = system_guard.signal_targets({10, 11}, signal.SIGTERM,
                                                 sender=lambda pid, sig: sent.append((pid, sig)))
        self.assertEqual(result, [10])
        self.assertEqual(sent, [(10, signal.SIGTERM)])

    def test_actionable_build_rows_rejects_generic_java(self):
        rows = [
            ProcessRow(10, "Java/Gradle", "/usr/bin/java language-server.jar", 0, 4 * system_guard.GIB, 0, "S"),
            ProcessRow(11, "Java/Gradle", "/usr/bin/java org.gradle.launcher.daemon.bootstrap.GradleDaemon", 0,
                       4 * system_guard.GIB, 0, "R"),
        ]
        with mock.patch.object(system_guard, "process_uid", return_value=1000), \
             mock.patch.object(system_guard.os, "getuid", return_value=1000):
            selected = system_guard.actionable_build_rows(rows, set())
        self.assertEqual([row.pid for row in selected], [11])

    def make_swap_storm_guardian(self):
        guardian = system_guard.Guardian.__new__(system_guard.Guardian)
        guardian.config = system_guard.GuardConfig()
        guardian.monitor = mock.Mock()
        guardian.ancestry = set()
        guardian.build_history = system_guard.deque([(940, 2 * system_guard.GIB)])
        guardian.swap_storm_since = None
        guardian.swap_storm_throttled = set()
        guardian.clock = mock.Mock(return_value=1000)
        guardian.monitor.processes = [
            ProcessRow(11, "Java/Gradle", "java org.gradle.launcher.daemon.bootstrap.GradleDaemon", 300,
                       4 * system_guard.GIB, 0, "R")]
        return guardian

    def test_build_swap_storm_requires_growth_low_memory_and_swap_out(self):
        guardian = self.make_swap_storm_guardian()
        snapshot = self.snapshot(1500 * system_guard.MIB, some=12, swap_out=100 * system_guard.MIB)
        snapshot["io_psi"] = {"full_avg10": 0}
        with mock.patch.object(system_guard, "process_uid", return_value=1000), \
             mock.patch.object(system_guard.os, "getuid", return_value=1000):
            detected = guardian._build_swap_storm(snapshot)
        self.assertIsNotNone(detected)
        healthy = self.snapshot(6 * system_guard.GIB, some=50, swap_out=200 * system_guard.MIB)
        healthy["io_psi"] = {"full_avg10": 50}
        self.assertIsNone(guardian._build_swap_storm(healthy))

    def test_build_swap_storm_throttles_before_termination(self):
        guardian = self.make_swap_storm_guardian()
        guardian.store = mock.Mock()
        snapshot = self.snapshot(1500 * system_guard.MIB, some=12, swap_out=100 * system_guard.MIB)
        snapshot["io_psi"] = {"full_avg10": 0}
        with mock.patch.object(system_guard, "process_uid", return_value=1000), \
             mock.patch.object(system_guard.os, "getuid", return_value=1000), \
             mock.patch.object(system_guard, "descendants", return_value={11}), \
             mock.patch.object(system_guard, "lower_build_priority", return_value=[11]), \
             mock.patch.object(system_guard, "signal_targets") as signals:
            self.assertIsNone(guardian._handle_build_swap_storm(snapshot))
        signals.assert_not_called()
        self.assertEqual(guardian.swap_storm_throttled, {11})

    def test_build_swap_storm_terminates_only_after_sustained_pressure(self):
        guardian = self.make_swap_storm_guardian()
        guardian.store = mock.Mock()
        guardian.store.write_incident.return_value = Path("/state/report.json")
        guardian.swap_storm_since = 980
        guardian.swap_storm_throttled = {11}
        snapshot = self.snapshot(1500 * system_guard.MIB, some=12, swap_out=100 * system_guard.MIB)
        snapshot["io_psi"] = {"full_avg10": 0}
        with mock.patch.object(system_guard, "process_uid", return_value=1000), \
             mock.patch.object(system_guard.os, "getuid", return_value=1000), \
             mock.patch.object(system_guard, "descendants", return_value={11}), \
             mock.patch.object(system_guard, "signal_targets", return_value=[11]) as signals, \
             mock.patch.object(system_guard, "notify") as notifier:
            result = guardian._handle_build_swap_storm(snapshot)
        signals.assert_called_once_with({11}, signal.SIGTERM)
        notifier.assert_called_once()
        self.assertEqual(result["kind"], "build-swap-relief")

    def test_compact_sample_keeps_only_top_groups(self):
        snapshot = {
            "timestamp": "now", "cpu_percent": 1, "iowait_percent": 0, "load": [1, 1, 1],
            "memory": {"available": 1, "total": 2}, "memory_psi": {}, "io_psi": {}, "cpu_psi": {},
            "blocked": [], "gpu": {},
            "groups": {f"group-{index}": {"rss": index, "swap": 0} for index in range(30)},
        }
        with mock.patch.object(system_guard, "current_boot_id", return_value="boot"):
            compact = system_guard.compact_sample(snapshot, "normal", [])
        self.assertEqual(len(compact["groups"]), 12)
        self.assertIn("group-29", compact["groups"])
        self.assertNotIn("group-0", compact["groups"])

    def test_gnome_shell_rss_does_not_use_whole_desktop_group(self):
        rows = [
            ProcessRow(10, "Desktop", "/usr/bin/gnome-shell", 0, 3 * system_guard.GIB, 0, "R"),
            ProcessRow(11, "Desktop", "/usr/lib/xorg/Xorg", 0, system_guard.GIB, 0, "S"),
        ]
        self.assertEqual(system_guard.gnome_shell_rss(rows), 3 * system_guard.GIB)

    def test_previous_boot_detects_stale_clean_exit(self):
        store = mock.Mock()
        store.read_marker.side_effect = [
            {"boot_id": "old", "epoch": 200, "state": "normal"},
            {"boot_id": "old", "epoch": 100},
        ]
        result = system_guard.previous_boot_was_unclean(store, "new")
        self.assertEqual(result["kind"], "unclean-reboot")

    def test_previous_boot_accepts_clean_exit_after_heartbeat(self):
        store = mock.Mock()
        store.read_marker.side_effect = [
            {"boot_id": "old", "epoch": 200}, {"boot_id": "old", "epoch": 201},
        ]
        self.assertIsNone(system_guard.previous_boot_was_unclean(store, "new"))

    def test_desktop_growth_triggers_before_global_pressure_threshold(self):
        guardian = system_guard.Guardian.__new__(system_guard.Guardian)
        guardian.config = system_guard.GuardConfig()
        guardian.monitor = mock.Mock()
        guardian.desktop_history = system_guard.deque()
        guardian.clock = mock.Mock(side_effect=[1000, 1060])
        guardian.monitor.processes = [
            ProcessRow(10, "Desktop", "/usr/bin/gnome-shell", 0, 2 * system_guard.GIB, 0, "R")]
        self.assertIsNone(guardian._desktop_risk({}, []))
        guardian.monitor.processes = [
            ProcessRow(10, "Desktop", "/usr/bin/gnome-shell", 0, 3 * system_guard.GIB, 0, "R")]
        self.assertIn("grew", guardian._desktop_risk({}, []))

    def test_stage_view_storm_triggers_graphics_protection(self):
        guardian = system_guard.Guardian.__new__(system_guard.Guardian)
        guardian.config = system_guard.GuardConfig()
        guardian.graphics_pressure_since = None
        guardian.clock = mock.Mock(return_value=1000)
        summary = [{"message": "Can't update stage views actor", "count": 6}]
        self.assertIn("stage-view", guardian._graphics_risk({"blocked": [], "io_psi": {}}, summary))

    def test_graphics_pressure_requires_sustained_io_and_blocked_graphics_task(self):
        guardian = system_guard.Guardian.__new__(system_guard.Guardian)
        guardian.config = system_guard.GuardConfig(graphics_sustain_seconds=15)
        guardian.graphics_pressure_since = None
        guardian.clock = mock.Mock(side_effect=[1000, 1016])
        snapshot = {"io_psi": {"full_avg10": 20}, "blocked": [{"command": "kworker+i915_flip"}]}
        self.assertIsNone(guardian._graphics_risk(snapshot, []))
        self.assertIn("blocked", guardian._graphics_risk(snapshot, []))

    def test_kernel_report_is_deduplicated_and_display_cooldown_is_silent(self):
        guardian = system_guard.Guardian.__new__(system_guard.Guardian)
        guardian.config = system_guard.GuardConfig(kernel_notification_cooldown_seconds=600)
        guardian.store = mock.Mock()
        guardian.store.write_incident.return_value = Path("/state/report.json")
        guardian.kernel_last_notified = {}
        summary = [{"fingerprint": "nvrm-xid", "count": 1,
                    "message": "NVRM: Xid (PCI:0000:01:00): 79"}]
        with mock.patch.object(system_guard.time, "time", side_effect=[1000, 1100, 1701]), \
             mock.patch.object(system_guard, "notify") as notifier:
            guardian._kernel_incident(summary)
            guardian._kernel_incident(summary)
            guardian._kernel_incident(summary)
        self.assertEqual(guardian.store.write_incident.call_count, 2)
        notifier.assert_not_called()

    def test_wechat_wxid_is_not_treated_as_gpu_xid(self):
        guardian = self.make_kernel_guardian()
        summary = [{"fingerprint": "wechat-temp", "count": 1,
                    "message": "missing /xwechat_files/wxid_example/ImageTemp/file"}]
        with mock.patch.object(system_guard.time, "time", return_value=1000), \
             mock.patch.object(system_guard, "notify") as notifier:
            guardian._kernel_incident(summary)
        guardian.store.write_incident.assert_not_called()
        notifier.assert_not_called()

    def test_non_actionable_kernel_event_records_without_popup(self):
        guardian = self.make_kernel_guardian()
        summary = [{"fingerprint": "oom", "count": 100,
                    "message": "out of memory warning"}]
        with mock.patch.object(system_guard.time, "time", return_value=1000), \
             mock.patch.object(system_guard, "notify") as notifier:
            guardian._kernel_incident(summary)
        guardian.store.write_incident.assert_called_once()
        notifier.assert_not_called()

    def make_kernel_guardian(self):
        guardian = system_guard.Guardian.__new__(system_guard.Guardian)
        guardian.config = system_guard.GuardConfig()
        guardian.store = mock.Mock()
        guardian.store.write_incident.return_value = Path("/state/report.json")
        guardian.kernel_last_notified = {}
        guardian.display_error_since = {}
        return guardian

    def invalid_head_summary(self, event_time):
        return [{"fingerprint": "nvrm-invalid-head", "count": 500,
                 "first_time": event_time, "last_time": event_time,
                 "message": "NVRM: GPU0 dispcmnCtrlCmdSystemGetVblankCounter_IMPL: invalid head number!"}]

    def test_invalid_head_is_silent_while_locked(self):
        guardian = self.make_kernel_guardian()
        with mock.patch.object(system_guard.time, "time", return_value=1000), \
             mock.patch.object(system_guard, "screen_lock_state", return_value="locked"), \
             mock.patch.object(system_guard, "notify") as notifier:
            guardian._kernel_incident(self.invalid_head_summary(1000))
        guardian.store.write_incident.assert_not_called()
        notifier.assert_not_called()

    def test_stale_lock_screen_error_does_not_alert_after_unlock(self):
        guardian = self.make_kernel_guardian()
        with mock.patch.object(system_guard.time, "time", side_effect=[1000, 1100]), \
             mock.patch.object(system_guard, "screen_lock_state", side_effect=["locked", "unlocked"]), \
             mock.patch.object(system_guard, "notify") as notifier:
            guardian._kernel_incident(self.invalid_head_summary(1000))
            guardian._kernel_incident(self.invalid_head_summary(1000))
        guardian.store.write_incident.assert_not_called()
        notifier.assert_not_called()

    def test_invalid_head_alerts_after_sixty_seconds_unlocked(self):
        guardian = self.make_kernel_guardian()
        with mock.patch.object(system_guard.time, "time", side_effect=[1000, 1061]), \
             mock.patch.object(system_guard, "screen_lock_state", return_value="unlocked"), \
             mock.patch.object(system_guard, "notify") as notifier:
            guardian._kernel_incident(self.invalid_head_summary(1000))
            guardian._kernel_incident(self.invalid_head_summary(1061))
        guardian.store.write_incident.assert_called_once()
        guardian.store.set_display_cooldown.assert_called_once()
        notifier.assert_not_called()


class IncidentStoreTests(unittest.TestCase):
    def test_writes_report_and_lists_it(self):
        with TemporaryDirectory() as temp:
            store = IncidentStore(Path(temp), max_sample_bytes=100)
            report = store.write_incident({"kind": "automatic-relief", "timestamp": "now"})
            self.assertTrue(report.exists())
            self.assertEqual(store.recent_incidents()[0]["kind"], "automatic-relief")

    def test_sample_file_is_bounded_by_rotation(self):
        with TemporaryDirectory() as temp:
            store = IncidentStore(Path(temp), max_sample_bytes=40)
            store.append_sample({"value": "a" * 30})
            store.append_sample({"value": "b" * 30})
            self.assertTrue(store.sample_path.exists())
            self.assertTrue(store.sample_path.with_suffix(".jsonl.1").exists())

    def test_display_cooldown_is_written_atomically(self):
        with TemporaryDirectory() as temp:
            store = IncidentStore(Path(temp))
            store.set_display_cooldown(1234, "test")
            payload = system_guard.json.loads(
                (Path(temp) / "display-cooldown.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["until_epoch"], 1234)


if __name__ == "__main__":
    unittest.main()
