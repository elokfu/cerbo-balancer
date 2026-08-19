import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cap_runtime_files.sh"


class RuntimeFileCapTest(unittest.TestCase):
    def test_caps_runtime_jsonl_and_leaves_csv_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "cerbo-balancer-state.json": (524288, 393216),
                "cerbo-balancer-config.json": (131072, 98304),
            }
            for name, (maximum, _) in files.items():
                path = root / name
                with path.open("w", encoding="utf-8") as stream:
                    sequence = 0
                    while stream.tell() <= maximum + 65536:
                        stream.write(json.dumps({"sequence": sequence, "payload": "x" * 512}) + "\n")
                        sequence += 1

            csv = root / "manual.csv"
            csv.write_text("keep,this\nunchanged,true\n", encoding="utf-8")
            events = root / "cerbo-balancer-events.jsonl"
            events.write_text('{"legacy":"untouched"}\n', encoding="utf-8")
            env = {**os.environ, "CERBO_BALANCER_RUNTIME_DIR": str(root)}
            subprocess.run(["sh", str(SCRIPT)], check=True, env=env)

            for name, (_, retained) in files.items():
                path = root / name
                self.assertLessEqual(path.stat().st_size, retained)
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertTrue(lines)
                for line in lines:
                    json.loads(line)
                self.assertIn("payload", json.loads(lines[-1]))
            self.assertEqual(csv.read_text(encoding="utf-8"), "keep,this\nunchanged,true\n")
            self.assertEqual(events.read_text(encoding="utf-8"), '{"legacy":"untouched"}\n')

    def test_small_files_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cerbo-balancer-state.json"
            original = '{"state":"NORMAL"}\n'
            path.write_text(original, encoding="utf-8")
            before = path.stat().st_mtime_ns
            env = {**os.environ, "CERBO_BALANCER_RUNTIME_DIR": directory}
            subprocess.run(["sh", str(SCRIPT)], check=True, env=env)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
