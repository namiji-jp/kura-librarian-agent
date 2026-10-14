import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kura_scan


class Reply:
    def __init__(self, result):
        self.result = result
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return json.dumps({"jsonrpc": "2.0", "id": 1,
                           "result": self.result}).encode()


class ScanRetryTests(unittest.TestCase):
    def setUp(self):
        self.mcp = kura_scan.Mcp.__new__(kura_scan.Mcp)
        self.mcp.session = None
        self.mcp.seq = 0
        self.mcp.auth = "Bearer test"

    def test_failed_read_attempted_three_times(self):
        calls = []
        def failing_rpc(*_):
            calls.append(True)
            return {"isError": True}
        with patch.object(self.mcp, "rpc", side_effect=failing_rpc), \
             patch.object(kura_scan.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "kept failing"):
                self.mcp.tool("operator_page_read", {"id": "test"})
        self.assertEqual(len(calls), 3)

    def test_failed_create_attempted_once(self):
        calls = []
        def failing_rpc(*_):
            calls.append(True)
            return {"isError": True}
        with patch.object(self.mcp, "rpc", side_effect=failing_rpc), \
             patch.object(kura_scan.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError,
                                        "operator_page_create: outcome unknown, not retried"):
                self.mcp.tool("operator_page_create", {"title": "test"})
        self.assertEqual(len(calls), 1)

    def test_dropped_update_connection_is_not_retried(self):
        with patch.object(kura_scan, "http_open", side_effect=OSError("dropped")) as send, \
             patch.object(kura_scan.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError,
                                        "operator_page_update: outcome unknown, not retried"):
                self.mcp.rpc("tools/call", {"name": "operator_page_update", "arguments": {}})
        self.assertEqual(send.call_count, 1)

    def test_dropped_list_connection_is_retried(self):
        with patch.object(kura_scan, "http_open",
                          side_effect=[OSError("dropped"), Reply({"items": []})]) as send, \
             patch.object(kura_scan.time, "sleep"):
            result = self.mcp.rpc("tools/call", {"name": "operator_page_list", "arguments": {}})
        self.assertEqual(result, {"items": []})
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
