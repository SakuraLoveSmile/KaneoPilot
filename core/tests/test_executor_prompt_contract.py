"""The dispatch controller must preserve the claimed task backend."""

from pathlib import Path
import unittest


class DispatchPromptBackendContractTests(unittest.TestCase):
    def test_prepare_and_confirm_use_claimed_backend_and_config_snapshot(self):
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "executor_prompts"
            / "dispatch.md"
        )
        prompt = prompt_path.read_text(encoding="utf-8")

        self.assertIn("pi_desktop", prompt)
        self.assertIn('"backend":<context.backend>', prompt)
        self.assertIn("context.exec_config 原样", prompt)
        self.assertNotIn('"backend":"pi"', prompt)


if __name__ == "__main__":
    unittest.main()
