import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "fix-monitor-layout.sh"
TMP_ROOT = PROJECT_ROOT / "tmp"


class MonitorLayoutTests(unittest.TestCase):
    def run_script(self, gdbus_output=None, gdbus_ok=True):
        TMP_ROOT.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=TMP_ROOT) as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "xrandr.calls"
            gdbus = bin_dir / "gdbus"
            gdbus.write_text(
                "#!/bin/sh\n" +
                (f"printf '%s\\n' \"{gdbus_output}\"\n" if gdbus_ok else "exit 1\n"),
                encoding="utf-8",
            )
            gdbus.chmod(0o755)
            loginctl = bin_dir / "loginctl"
            loginctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            loginctl.chmod(0o755)
            xrandr = bin_dir / "xrandr"
            xrandr.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$XRANDR_MARKER\"\n"
                "[ \"$1\" != --query ] || printf '%s\\n' 'HDMI-1-0 connected 2560x1440+0+160'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            xrandr.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["XRANDR_MARKER"] = str(marker)
            result = subprocess.run([str(SCRIPT)], env=environment, capture_output=True, text=True, check=False)
            calls = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
            return result, calls

    def test_locked_screen_never_queries_xrandr(self):
        result, calls = self.run_script("(true,)")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_unknown_lock_state_fails_closed_without_xrandr(self):
        result, calls = self.run_script(gdbus_ok=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_unlocked_screen_preserves_existing_layout_fix(self):
        result, calls = self.run_script("(false,)")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "--query")
        self.assertIn("--output HDMI-1-0", calls[1])


if __name__ == "__main__":
    unittest.main()
