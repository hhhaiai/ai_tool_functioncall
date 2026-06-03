"""Tests for headroom-style context compression."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.gateway_headroom import (
    _deduplicate_repetitive_lines,
    _approx_tokens,
    _body_tokens,
    _is_json_array,
    _looks_like_code,
    _looks_like_log,
    _smart_crush_json_array,
    _summarize_code,
    _key_log_lines,
    headroom_compress,
)


class JsonArrayDetectionTests(unittest.TestCase):
    def test_simple_array_of_dicts(self):
        text = json.dumps([{"a": 1}, {"a": 2}, {"a": 3}])
        self.assertTrue(_is_json_array(text))

    def test_nested_array(self):
        text = json.dumps([[1, 2], [3, 4], [5, 6]])
        self.assertTrue(_is_json_array(text))

    def test_empty_array(self):
        self.assertFalse(_is_json_array("[]"))

    def test_object(self):
        self.assertFalse(_is_json_array('{"a": 1}'))

    def test_garbage(self):
        self.assertFalse(_is_json_array("not json at all"))

    def test_with_leading_text(self):
        text = "Here is the result: " + json.dumps([{"a": 1}, {"a": 2}])
        self.assertFalse(_is_json_array(text))


class CodeDetectionTests(unittest.TestCase):
    def test_python_function(self):
        text = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        self.assertTrue(_looks_like_code(text))

    def test_typescript(self):
        text = "function foo() {\n  return 1;\n}\nconst bar = 2;\n"
        self.assertTrue(_looks_like_code(text))

    def test_plain_prose(self):
        text = "The quick brown fox jumps over the lazy dog. " * 5
        self.assertFalse(_looks_like_code(text))

    def test_short_text(self):
        self.assertFalse(_looks_like_code("def foo(): pass"))


class LogDetectionTests(unittest.TestCase):
    def test_typical_log(self):
        lines = ["2026-01-01 INFO starting up"] * 5 + ["2026-01-01 ERROR connection failed"] * 3
        text = "\n".join(lines)
        self.assertTrue(_looks_like_log(text))

    def test_plain_prose(self):
        self.assertFalse(_looks_like_log("The quick brown fox jumps over the lazy dog. " * 5))

    def test_short_text(self):
        self.assertFalse(_looks_like_log("ERROR oops"))


class SmartCrusherTests(unittest.TestCase):
    def test_drops_middle_rows(self):
        rows = [{"i": i} for i in range(100)]
        text = json.dumps(rows)
        out = _smart_crush_json_array(text, max_items=10)
        parsed = json.loads(out)
        # head (5) + sentinel (1) + tail (5) = 11
        self.assertEqual(len(parsed), 11)
        # Sentinel present
        self.assertTrue(any(isinstance(r, dict) and "_headroom_dropped" in r for r in parsed))
        # Head and tail preserved
        self.assertEqual(parsed[0]["i"], 0)
        self.assertEqual(parsed[-1]["i"], 99)

    def test_no_op_for_short_array(self):
        rows = [{"i": i} for i in range(5)]
        text = json.dumps(rows)
        out = _smart_crush_json_array(text, max_items=10)
        self.assertEqual(out, text)

    def test_invalid_json_unchanged(self):
        self.assertEqual(_smart_crush_json_array("not json", max_items=5), "not json")


class CodeSummaryTests(unittest.TestCase):
    def test_keeps_head_and_tail(self):
        lines = [f"line {i}" for i in range(500)]
        text = "\n".join(lines)
        out = _summarize_code(text, head_lines=10, tail_lines=10)
        self.assertIn("line 0", out)
        self.assertIn("line 9", out)
        self.assertIn("line 499", out)
        self.assertIn("line 490", out)
        self.assertNotIn("line 100", out)
        self.assertIn("headroom dropped", out)

    def test_short_code_unchanged(self):
        text = "\n".join([f"line {i}" for i in range(5)])
        out = _summarize_code(text, head_lines=10, tail_lines=10)
        self.assertEqual(out, text)


class LogKeyingTests(unittest.TestCase):
    def test_keeps_unique_error_lines(self):
        lines = (
            ["INFO starting up"] * 50
            + ["ERROR connection refused"] * 5
            + ["WARN retrying"] * 10
            + ["ERROR timeout on /foo"] * 3
        )
        text = "\n".join(lines)
        out = _key_log_lines(text, keep_lines=10)
        self.assertIn("ERROR connection refused", out)
        self.assertIn("WARN retrying", out)
        self.assertIn("ERROR timeout", out)
        self.assertIn("headroom kept", out)

    def test_fallback_to_tail_when_no_signal(self):
        text = "line a\n" * 50
        out = _key_log_lines(text, keep_lines=5)
        self.assertIn("line a", out)
        # Tail content is preserved (head is dropped)
        self.assertLess(out.count("line a"), text.count("line a"))


class EndToEndCompressionTests(unittest.TestCase):
    def _make_huge_request(self) -> dict:
        return {
            "model": "mimo-v2.5-pro",
            "max_tokens": 2048,
            "system": "x" * 200_000,
            "messages": [
                {"role": "user", "content": "first user"},
                {"role": "assistant", "content": "first assistant"},
            ]
            + [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"id{i}",
                            "content": json.dumps([{"row": j, "data": "x" * 100} for j in range(200)]),
                        }
                    ],
                }
                for i in range(20)
            ]
            + [{"role": "user", "content": "final question"}],
        }

    def test_huge_request_compresses_under_target(self):
        body = self._make_huge_request()
        before = _body_tokens(body)
        out = headroom_compress(body, target_tokens=10_000)
        after = _body_tokens(out)
        self.assertLess(after, before)
        self.assertLessEqual(after, 10_000)

    def test_small_request_unchanged(self):
        body = {
            "model": "m",
            "system": "short",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = headroom_compress(body, target_tokens=10_000)
        self.assertEqual(out, body)

    def test_message_history_trimmed_when_tool_compression_insufficient(self):
        body = {
            "model": "m",
            "system": "x" * 50_000,
            "messages": [{"role": "user", "content": "msg"} for _ in range(50)],
        }
        out = headroom_compress(body, target_tokens=2_000)
        # History is trimmed to keep_recent_messages (default 12) and
        # system eventually replaced by the marker.
        self.assertLess(_body_tokens(out), _body_tokens(body))
        # System either shrunk or replaced
        sys_v = out.get("system", "")
        if isinstance(sys_v, list):
            sys_text = " ".join(b.get("text", "") for b in sys_v if isinstance(b, dict))
        else:
            sys_text = str(sys_v)
        self.assertTrue(
            "[headroom]" in sys_text or len(sys_text) < len("x" * 50_000),
            "system should be shrunk or replaced when over budget",
        )

    def test_tool_result_compression_marks_block(self):
        body = {
            "model": "m",
            "system": "",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "abc",
                            "content": json.dumps([{"i": i} for i in range(500)]),
                        }
                    ],
                }
            ],
        }
        out = headroom_compress(body, target_tokens=200)
        # Tool result should be crushed
        msg = out["messages"][0]
        block = msg["content"][0]
        self.assertIn("_headroom_dropped", block["content"])
        self.assertIn("gateway_context", block)


if __name__ == "__main__":
    unittest.main()


class DeduplicationTests(unittest.TestCase):
    def test_collapses_repeated_lines(self):
        text = "line a\n" * 50 + "line b\n" * 50
        out = _deduplicate_repetitive_lines(text, keep_first=3, keep_last=3)
        self.assertIn("line a", out)
        self.assertIn("line b", out)
        self.assertIn("headroom deduplicated", out)
        self.assertLess(out.count("line a"), text.count("line a"))

    def test_short_text_unchanged(self):
        text = "line a\nline b\nline c"
        out = _deduplicate_repetitive_lines(text, keep_first=5, keep_last=5)
        self.assertEqual(out, text)


class HistoryTrimTests(unittest.TestCase):
    def test_trim_to_keep_last(self):
        from src.gateway_headroom import _trim_messages
        msgs = [{"role": "user", "content": str(i)} for i in range(20)]
        out = _trim_messages(msgs, keep_last=5)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[-1]["content"], "19")

    def test_no_trim_when_short(self):
        from src.gateway_headroom import _trim_messages
        msgs = [{"role": "user", "content": str(i)} for i in range(3)]
        out = _trim_messages(msgs, keep_last=5)
        self.assertEqual(len(out), 3)
