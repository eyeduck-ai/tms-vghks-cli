import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import cli, cli_impl


class CliFacadeTests(unittest.TestCase):
    def test_public_entrypoints_delegate_to_impl(self):
        self.assertIs(cli.build_parser().__class__, cli_impl.build_parser().__class__)
        self.assertEqual(cli.__all__, ["build_parser", "main"])

    def test_internal_attribute_assignment_is_forwarded_for_legacy_tests(self):
        original = cli_impl.TmsSession
        replacement = object()
        try:
            cli.TmsSession = replacement
            self.assertIs(cli_impl.TmsSession, replacement)
        finally:
            cli.TmsSession = original


if __name__ == "__main__":
    unittest.main()
