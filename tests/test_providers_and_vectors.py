import json
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

from src import cloud_client, embedder, lancedb_store
from src.llm_router import Llama70BRouter


def completion(content):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
    )


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        cloud_client._ollama_client = None
        cloud_client._openai_client = None

    def test_ollama_success_does_not_call_nim(self):
        ollama = Mock()
        ollama.chat.return_value = types.SimpleNamespace(
            message=types.SimpleNamespace(content=" primary "), done_reason="stop"
        )
        with patch.object(cloud_client, "_get_ollama_client", return_value=ollama), patch.object(
            cloud_client, "_get_openai_client"
        ) as nim:
            result = cloud_client.chat_completion("system", "user")
        self.assertEqual(result, "primary")
        nim.assert_not_called()
        self.assertFalse(ollama.chat.call_args.kwargs["think"])

    def test_three_ollama_failures_fall_back_to_nim(self):
        ollama = Mock()
        ollama.chat.side_effect = RuntimeError("offline")
        nim = Mock()
        nim.chat.completions.create.return_value = completion(" fallback ")
        with patch.object(cloud_client, "_get_ollama_client", return_value=ollama), patch.object(
            cloud_client, "_get_openai_client", return_value=nim
        ), patch.object(cloud_client.time, "sleep"):
            result = cloud_client.chat_completion(
                "system", "user", nim_model="nim-test", response_format={"type": "json_object"}
            )
        self.assertEqual(result, "fallback")
        self.assertEqual(ollama.chat.call_count, 3)
        kwargs = nim.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "nim-test")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})


class MultiProviderRouterTests(unittest.TestCase):
    def test_provider_failure_uses_next_provider(self):
        router = object.__new__(Llama70BRouter)
        first, second, nim = Mock(), Mock(), Mock()
        first.chat.completions.create.side_effect = RuntimeError("rate limited")
        second.chat.completions.create.return_value = completion("second")
        router._providers = [("first", first, "m1"), ("second", second, "m2")]
        router._nim = nim
        router._nim_model = "nim"
        router._rr_index = 0
        self.assertEqual(router.generate("s", "u"), "second")
        nim.chat.completions.create.assert_not_called()
        self.assertEqual(router._rr_index, 0)

    def test_all_provider_failures_use_nim(self):
        router = object.__new__(Llama70BRouter)
        provider, nim = Mock(), Mock()
        provider.chat.completions.create.return_value = completion("")
        nim.chat.completions.create.return_value = completion("nim")
        router._providers = [("first", provider, "m1")]
        router._nim = nim
        router._nim_model = "nim-model"
        router._rr_index = 0
        self.assertEqual(router.generate("s", "u"), "nim")


class EmbeddingAndVectorBoundaryTests(unittest.TestCase):
    def test_hash_fallback_is_deterministic_and_normalized(self):
        with patch.object(embedder, "_embed_remote", return_value=None), patch.object(
            embedder, "_embed_local_st", return_value=None
        ):
            first = embedder.embed_texts(["inflation bonds"], dim=32)
            second = embedder.embed_texts(["inflation bonds"], dim=32)
        np.testing.assert_array_equal(first, second)
        self.assertEqual(first.shape, (1, 32))
        self.assertAlmostEqual(float(np.linalg.norm(first[0])), 1.0, places=6)

    def test_dimension_mismatch_uses_hash_fallback(self):
        wrong = np.ones((1, 8), dtype=np.float32)
        with patch.object(embedder, "_embed_remote", return_value=wrong), patch.object(
            embedder, "_embed_local_st", return_value=None
        ):
            out = embedder.embed_texts(["test"], dim=16)
        self.assertEqual(out.shape, (1, 16))
        self.assertEqual(embedder.backend_name(), "hash-fallback")

    def test_empty_lancedb_search_returns_empty_without_embedding(self):
        with patch.object(lancedb_store, "_get_table", return_value=None), patch.object(
            lancedb_store, "embed_one"
        ) as embed:
            self.assertEqual(lancedb_store.search_hybrid("query"), [])
        embed.assert_not_called()

    def test_semantic_search_wrapper_preserves_result_order(self):
        rows = [{"video_id": "b"}, {"video_id": "a"}]
        views = [{"video_id": "b"}, {"video_id": "a"}]
        with patch.object(lancedb_store, "search_hybrid", return_value=rows), patch.object(
            lancedb_store, "hydrate_views", return_value=views
        ) as hydrate:
            result = json.loads(lancedb_store.semantic_search_macro("rates", top_k=2))
        hydrate.assert_called_once_with(["b", "a"])
        self.assertEqual(result["results"], views)
