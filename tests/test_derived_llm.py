import unittest
from unittest.mock import patch

from src.derived_llm import DerivedLLMRequest, complete_derived
from src.llm_providers import CompletionResult, ProviderAttempt


class DerivedLlmBoundaryTests(unittest.TestCase):
    def test_request_parameters_and_result_metadata_are_preserved(self):
        attempt = ProviderAttempt("nim", "nim-x", 1, 12.5, True)
        completion = CompletionResult("report", "nim", "nim-x", 14.0, (attempt,))
        request = DerivedLLMRequest(
            pipeline="cio", system="system", user="full prompt", max_tokens=8192,
            temperature=0.3, nim_model="nim-x", ollama_attempts=4,
        )

        with patch("src.derived_llm._chat_completion_result", return_value=completion) as call:
            result = complete_derived(request)

        call.assert_called_once_with(
            system="system", user="full prompt", max_tokens=8192, temperature=0.3,
            model=None, nim_model="nim-x", response_format=None, ollama_attempts=4,
        )
        self.assertEqual(result.pipeline, "cio")
        self.assertEqual(result.content, "report")
        self.assertEqual(result.provider, "nim")
        self.assertEqual(result.model, "nim-x")
        self.assertEqual(result.latency_ms, 14.0)
        self.assertEqual(result.attempts, (attempt,))

    def test_provider_exception_propagates_without_an_extra_retry(self):
        request = DerivedLLMRequest(
            pipeline="daily", system="s", user="u", max_tokens=4096,
            temperature=0.2, nim_model="nim", ollama_attempts=5,
        )
        with patch(
            "src.derived_llm._chat_completion_result",
            side_effect=RuntimeError("providers exhausted"),
        ) as call:
            with self.assertRaisesRegex(RuntimeError, "providers exhausted"):
                complete_derived(request)
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
