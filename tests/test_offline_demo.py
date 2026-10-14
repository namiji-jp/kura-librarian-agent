import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import offline_demo


class OfflineDemoTests(unittest.TestCase):
    def test_full_offline_sequence_uses_extracted_jev_and_publisher_path(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            offline_demo.run()
        result = json.loads(output.getvalue())
        self.assertEqual(result["decision_inputs"], 2)
        self.assertEqual(result["abstentions"], 1)
        self.assertEqual(result["fake_kura_append_calls"], 1)
        self.assertEqual(result["fake_kura_report_writes"], 1)
        self.assertEqual(result["fixture_decision_calls"], 2)
        self.assertEqual(result["external_provider_calls"], 0)
        self.assertIn("PUBLISHED", result["fake_publish_result"])
        self.assertIn("Synthetic", result["fixture_notice"])
        self.assertIn("Coastal route project", result["chosen_link_target"])
