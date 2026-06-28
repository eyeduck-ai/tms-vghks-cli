import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.export_pacing import ExportPacer, export_pacing_options_from_cli


class ExportPacingTests(unittest.TestCase):
    def test_cli_options_default_to_random_delay(self):
        options = export_pacing_options_from_cli(
            delay_min_ms=None,
            delay_max_ms=None,
        )

        self.assertTrue(options.enabled)
        self.assertEqual(options.min_ms, 400)
        self.assertEqual(options.max_ms, 1400)

    def test_no_random_delay_disables_sleep(self):
        slept = []
        options = export_pacing_options_from_cli(
            delay_min_ms=400,
            delay_max_ms=1400,
            no_random_delay=True,
            delay_seed=7,
        )
        pacer = ExportPacer(options, sleep_func=slept.append)

        self.assertEqual(pacer.sleep("test"), 0.0)
        self.assertEqual(slept, [])
        self.assertEqual(pacer.summary()["enabled"], False)

    def test_rejects_invalid_delay_ranges(self):
        with self.assertRaises(ValueError):
            export_pacing_options_from_cli(delay_min_ms=-1, delay_max_ms=100)
        with self.assertRaises(ValueError):
            export_pacing_options_from_cli(delay_min_ms=200, delay_max_ms=100)

    def test_seed_makes_delay_reproducible_without_real_sleep(self):
        observed_a = []
        observed_b = []
        options_a = export_pacing_options_from_cli(delay_min_ms=100, delay_max_ms=200, delay_seed=42)
        options_b = export_pacing_options_from_cli(delay_min_ms=100, delay_max_ms=200, delay_seed=42)
        pacer_a = ExportPacer(options_a, sleep_func=observed_a.append)
        pacer_b = ExportPacer(options_b, sleep_func=observed_b.append)

        pacer_a.sleep("a")
        pacer_b.sleep("b")

        self.assertEqual(observed_a, observed_b)
        self.assertEqual(pacer_a.summary()["sleep_count"], 1)
        self.assertGreater(pacer_a.summary()["total_sleep_seconds"], 0)
        self.assertEqual(pacer_a.summary()["label_counts"], {"a": 1})


if __name__ == "__main__":
    unittest.main()
