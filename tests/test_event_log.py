import json
import tempfile
import unittest
from pathlib import Path

from event_log import MAX_EVENT_RECORDS, append_event, trim_event_log


class EventLogTests(unittest.TestCase):
    def test_append_retains_only_the_latest_1000_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            for index in range(MAX_EVENT_RECORDS + 25):
                append_event(path, {"index": index})

            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(events), MAX_EVENT_RECORDS)
            self.assertEqual(events[0]["index"], 25)
            self.assertEqual(events[-1]["index"], MAX_EVENT_RECORDS + 24)

    def test_startup_compaction_trims_existing_log_without_new_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(
                json.dumps({"index": index}) + "\n"
                for index in range(MAX_EVENT_RECORDS + 10)
            ))

            trim_event_log(path)

            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(events), MAX_EVENT_RECORDS)
            self.assertEqual(events[0]["index"], 10)
            self.assertEqual(events[-1]["index"], MAX_EVENT_RECORDS + 9)

    def test_empty_and_under_limit_logs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            trim_event_log(path)
            append_event(path, {"type": "one"})
            before = path.read_bytes()

            trim_event_log(path)

            self.assertEqual(path.read_bytes(), before)
