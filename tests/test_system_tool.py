import unittest

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

    def test_grouping(self):
        self.assertEqual(system_tool.process_group("rviz2 -d view.rviz", "rviz2", ""), "ROS/RViz")
        self.assertEqual(system_tool.process_group("/opt/google/chrome/chrome", "chrome", ""), "Chrome")

    def test_diagnose_memory_pressure(self):
        snapshot = {
            "memory": {"available": 100, "total": 1000, "swap_out_per_sec": 30 * 1024 * 1024},
            "memory_psi": {"some_avg10": 8}, "io_psi": {"full_avg10": 0},
            "cpu_psi": {"some_avg10": 0}, "cpu_percent": 10, "top_cpu": [], "gpu": {}, "alerts": [],
        }
        self.assertEqual(system_tool.diagnose(snapshot)[0]["title"], "内存换页")


if __name__ == "__main__":
    unittest.main()
