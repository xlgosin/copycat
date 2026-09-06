import json
import tempfile
import unittest
from pathlib import Path

from alphafox_probe import MetadataParser, initialize, save_result


class AlphaFoxProbeTests(unittest.TestCase):
    def test_public_metadata_parser(self):
        parser = MetadataParser()
        parser.feed('<title>测试策略 | AlphaFox</title><meta name="description" content="近窗 ROI 1%">')
        self.assertEqual(parser.title, "测试策略 | AlphaFox")
        self.assertEqual(parser.description, "近窗 ROI 1%")

    def test_snapshot_only_records_payload_change_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "probe.db"
            initialize(db)
            body = {"items": [{"id": "one", "time": "2026-09-06T00:00:00Z"}]}
            self.assertTrue(save_result(db, "trader", "orders", "now", 200, 10, body))
            self.assertFalse(save_result(db, "trader", "orders", "later", 200, 11, body))

            import sqlite3
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM probe_runs").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
                saved = json.loads(con.execute("SELECT body FROM snapshots").fetchone()[0])
            self.assertEqual(saved, body)


if __name__ == "__main__":
    unittest.main()
