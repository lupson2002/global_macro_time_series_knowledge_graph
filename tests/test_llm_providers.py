import unittest
from unittest.mock import Mock

from src.llm_providers import ProviderChainError, ProviderStep, execute_provider_chain


class ProviderChainTests(unittest.TestCase):
    def test_retries_then_falls_back_with_attempt_history(self):
        primary = Mock(side_effect=[RuntimeError("busy"), ""])
        fallback = Mock(return_value=" answer ")
        sleep = Mock()

        result = execute_provider_chain(
            [
                ProviderStep("primary", "m1", primary, max_attempts=2, retry_delay_s=1.5),
                ProviderStep("fallback", "m2", fallback),
            ],
            sleep=sleep,
        )

        self.assertEqual(result.content, "answer")
        self.assertEqual((result.provider, result.model), ("fallback", "m2"))
        self.assertEqual([attempt.succeeded for attempt in result.attempts], [False, False, True])
        self.assertIn("RuntimeError: busy", result.attempts[0].error)
        self.assertIn("empty response", result.attempts[1].error)
        sleep.assert_called_once_with(1.5)

    def test_success_stops_chain(self):
        fallback = Mock()
        result = execute_provider_chain(
            [ProviderStep("primary", "m1", lambda: "ok"), ProviderStep("fallback", "m2", fallback)]
        )
        self.assertEqual(result.content, "ok")
        fallback.assert_not_called()

    def test_exhaustion_exposes_attempts(self):
        with self.assertRaises(ProviderChainError) as raised:
            execute_provider_chain([ProviderStep("primary", "m1", lambda: "")])
        self.assertEqual(len(raised.exception.attempts), 1)
        self.assertFalse(raised.exception.attempts[0].succeeded)

    def test_rejects_invalid_retry_policy(self):
        with self.assertRaises(ValueError):
            ProviderStep("primary", "m1", lambda: "ok", max_attempts=0)
