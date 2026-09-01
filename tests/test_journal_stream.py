import unittest

from journal_stream import EventAggregator, fingerprint


class JournalTests(unittest.TestCase):
    def test_fingerprint_removes_addresses_and_large_numbers(self):
        self.assertEqual(
            fingerprint("NVRM address 0x123abc pid 12345"),
            fingerprint("NVRM address 0xff00 pid 99999"),
        )

    def test_aggregates_and_expires_duplicates(self):
        aggregate = EventAggregator(window_seconds=60)
        aggregate.add({"time": 100, "fingerprint": "nvrm", "message": "first"})
        aggregate.add({"time": 110, "fingerprint": "nvrm", "message": "second"})
        summary = aggregate.summary(120)[0]
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["first_time"], 100)
        self.assertEqual(summary["last_time"], 110)
        self.assertEqual(aggregate.summary(171), [])


if __name__ == "__main__":
    unittest.main()
