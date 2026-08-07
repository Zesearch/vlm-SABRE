from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vlmbench.pipeline.schema_validation import (
    OpenAISchemaCompatibilityError,
    load_task_schema_file,
    validate_openai_strict_model,
    validate_task_schema_source,
)
from vlmbench.recipes.schema import BenchmarkDesign


REFERENCE_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/vlmbench/agent/templates/base_schema.py"
).read_text(encoding="utf-8")


class TaskSchemaValidationTests(unittest.TestCase):
    def test_reference_schema_is_strict_and_downstream_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.py"
            path.write_text(REFERENCE_SCHEMA, encoding="utf-8")
            schema = load_task_schema_file(path)

        strict = validate_openai_strict_model(schema)
        self.assertEqual(set(strict["required"]), set(strict["properties"]))
        self.assertNotIn("QuestionSpec", strict["$defs"])

    def test_generic_dynamic_options_map_is_rejected_locally(self) -> None:
        with self.assertRaisesRegex(
            OpenAISchemaCompatibilityError,
            "dynamic object maps",
        ):
            validate_openai_strict_model(BenchmarkDesign)

    def test_generated_schema_cannot_execute_functions(self) -> None:
        malicious = REFERENCE_SCHEMA + "\n\ndef run_command():\n    pass\n"
        with self.assertRaisesRegex(ValueError, "declarative class definitions"):
            validate_task_schema_source(malicious)

    def test_task_schema_cannot_add_top_level_fields(self) -> None:
        incompatible = REFERENCE_SCHEMA.replace(
            "class TaskDesign(BenchmarkDesign):\n",
            "class TaskDesign(BenchmarkDesign):\n    scene: str\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.py"
            path.write_text(incompatible, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "top-level envelope"):
                load_task_schema_file(path)


if __name__ == "__main__":
    unittest.main()
