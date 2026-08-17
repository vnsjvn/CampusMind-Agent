import json
import unittest
from pathlib import Path


class RagasDatasetTests(unittest.TestCase):
    def test_curated_dataset_has_required_reference_fields(self):
        path = Path(__file__).resolve().parents[1] / "app" / "rag_eval" / "mindbridge-ragas-eval.json"
        rows = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(rows), 12)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        for row in rows:
            self.assertTrue(row["question"].strip())
            self.assertGreaterEqual(len(row["reference"].strip()), 30)


if __name__ == "__main__":
    unittest.main()
