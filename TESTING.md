# Testing

Wave 1 characterization tests preserve the production baseline before structural refactoring.

## Run

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q main.py src scripts publish_all_blogs.py tests
```

The suite uses temporary directories and mocks. It does not require network access, API keys, cookies, the production SQLite database, the production Obsidian vault, or the production LanceDB index.

## Contract coverage

- `test_config.py`: defaults, legacy environment names, override precedence and startup validation.
- `test_extraction.py`: JSON sanitization, backlink normalization, metadata overrides, soft schema validation, full-transcript retry.
- `test_local_llm_client.py`: full single-shot delivery for a transcript larger than the former truncation threshold.
- `test_llm_response.py`: response stage ordering, trusted overrides, one recovery limit, and soft validation.
- `test_pipeline_contracts.py`: macro relevance rules, skipped-video idempotency, channel tier filtering.
- `test_derived_pipelines.py`: Daily sentiment, CIO evidence/table rendering, Telegram dispatch/chunking, Insight headline/node contracts.
- `test_json_utils.py`: malformed/non-array JSON fallback and explicit native-list compatibility mode.
- `test_email_delivery.py`: plain/HTML MIME shape, attachment filtering, SMTP options, and pipeline-specific fallback policies.
- `test_pipeline_service.py`: full-transcript handoff, typed stage failures, IP-block abort, delay policy and retryable partial storage.
- `test_main_cli.py`: CLI defaults, target ordering/deduplication, tier selection, backfill ID handling, delay sequencing, and exit status.
- `test_domain.py`: read-only schema sections, malformed-section isolation, list copy safety, and canonical vector projection.
- `test_projections.py`: canonical LanceDB projection and explicit false-upsert failure.
- `test_reconciliation.py`: drift planning, read-only audit, explicit apply guard, backup integrity, missing-only repair, and batched vector repair ordering.
- `test_providers_and_vectors.py`: provider failover, embedding boundaries, vector search, and single-transaction batch upsert.
- `test_refactoring_audit.py`: deterministic AST metrics, risk ordering, and JSON output for refactoring baselines.
- `test_storage.py`: legacy SQLite migration, upsert semantics, JSON round-trip, Obsidian YAML/backlinks.
- `test_mcp_security.py`: SQL allow-list, recursive CTE rejection, result cap, SQLite read-only URI.
- `test_llm_providers.py`: provider retry/failover limits, empty response handling and attempt metadata.
- `test_providers_and_vectors.py`: Ollama/NIM failover, multi-provider round-robin/metadata, deterministic embedding fallback, LanceDB empty/search boundaries.

## Safety rules for new tests

- Use `tempfile.TemporaryDirectory` for SQLite, Obsidian and vector state.
- Mock all LLM, embedding, email, Telegram, browser and YouTube calls.
- Never read `.env`, `cookies.txt` or files under production `data/`, `logs/`, `reports/` or `obsidian_vault/`.
- Add a regression test before fixing a discovered behavior.
- Compare deterministic DB/Markdown fields; do not snapshot timestamps or provider-generated prose.
