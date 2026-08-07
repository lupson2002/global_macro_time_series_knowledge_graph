import unittest

from src.config import load_settings


class SettingsDefaultsTests(unittest.TestCase):
    def test_defaults_preserve_current_runtime_contract(self):
        settings = load_settings({})
        self.assertEqual(settings.llm.nim_base_url, "http://localhost:8000")
        self.assertEqual(settings.llm.tier2_model, "deepseek-ai/deepseek-v4-flash")
        self.assertEqual(settings.llm.tier3_model, "deepseek-ai/deepseek-v4-flash")
        self.assertEqual(settings.llm.insight_model, settings.llm.tier3_model)
        self.assertEqual(settings.llm.ollama_base_url, "https://ollama.com")
        self.assertEqual(settings.llm.ollama_model, "deepseek-v4-flash:0731-cloud")
        self.assertEqual(settings.telegram.ollama_base_url, "https://ollama.com/v1")
        self.assertEqual(settings.telegram.ollama_model, "llama3.1:70b")
        self.assertEqual(settings.embedding.dimension, 256)
        self.assertEqual(settings.youtube.cookies_file, "cookies.txt")
        self.assertEqual(settings.email.smtp_port, 465)

    def test_existing_environment_names_override_defaults(self):
        settings = load_settings({
            "NIM_BASE_URL": "https://nim.example/v1/",
            "TIER2_MODEL": "tier2",
            "TIER3_MODEL": "tier3",
            "INSIGHT_MODEL": "insight",
            "OLLAMA_PRO_MODEL": "shared-model",
            "EMBEDDING_DIM": "4096",
            "EMAIL_TO": "a@example.com, b@example.com",
            "YOUTUBE_COOKIES_FILE": "/tmp/cookies.txt",
        })
        self.assertEqual(settings.llm.nim_base_url, "https://nim.example/v1")
        self.assertEqual(settings.llm.tier2_model, "tier2")
        self.assertEqual(settings.llm.tier3_model, "tier3")
        self.assertEqual(settings.llm.insight_model, "insight")
        self.assertEqual(settings.llm.ollama_model, "shared-model")
        self.assertEqual(settings.telegram.ollama_model, "shared-model")
        self.assertEqual(settings.embedding.dimension, 4096)
        self.assertEqual(settings.email.recipients, ("a@example.com", "b@example.com"))
        self.assertEqual(settings.youtube.cookies_file, "/tmp/cookies.txt")

    def test_email_legacy_fallbacks_remain_supported(self):
        settings = load_settings({"SMTP_USER": "legacy@example.com", "SMTP_PASS": "secret"})
        self.assertEqual(settings.email.user, "legacy@example.com")
        self.assertEqual(settings.email.password, "secret")
        self.assertEqual(settings.email.recipients, ("legacy@example.com",))


class SettingsValidationTests(unittest.TestCase):
    def test_rejects_invalid_numeric_values(self):
        for name, value in (
            ("EMBEDDING_DIM", "0"),
            ("SMTP_PORT", "not-a-number"),
            ("TIER2_TIMEOUT", "-1"),
            ("MAX_TOOL_ITER", "0"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                load_settings({name: value})

    def test_rejects_invalid_urls_and_transport(self):
        for env, field in (
            ({"NIM_BASE_URL": "localhost:8000"}, "NIM_BASE_URL"),
            ({"OLLAMA_PRO_BASE_URL": "ftp://example.com"}, "OLLAMA_PRO_BASE_URL"),
            ({"MCP_TRANSPORT": "socket"}, "MCP_TRANSPORT"),
        ):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, field):
                load_settings(env)
