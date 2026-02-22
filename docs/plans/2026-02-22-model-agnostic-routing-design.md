# Emergent Model-Agnostic Routing Design

Date: 2026-02-22

## Goal

Make Emergent provider/model agnostic while minimizing token spend for orchestration workloads.

## Decisions

- Keep safety tiers and model tiers separate concerns.
- Add model tiers (`fast`, `default`, `strong`, `summary`) and deterministic routing.
- Support providers: `anthropic`, `openrouter`, `ollama`.
- Keep runtime safety guards fixed and non-user-modifiable.

## Configuration Model

New `agent` config shape:

- `providers.<name>`: connection settings (`api_key_env`, `base_url`).
- `models.<tier>`: `provider`, `model`, `max_tokens`, `input_per_mtok`, `output_per_mtok`.
- `routing_enabled`: on/off switch for tier routing.

Legacy keys (`provider`, `model`, `summary_provider`, `haiku_model`, `ollama_base_url`, `max_tokens`) remain supported through fallback parsing and compatibility properties.

## Runtime Routing

`ModelRouter` chooses a tier using deterministic heuristics:

- long prompt or strong keywords -> `strong`
- short command-like prompts -> `fast`
- safety-sensitive tool landscape (tier2+) -> at least `default`
- otherwise -> `default`

Routing can be disabled, forcing `default` tier.

## LLM Client Layer

- Existing: `AnthropicLLMClient`, `OllamaLLMClient`
- Added: `OpenAICompatibleLLMClient` for OpenRouter/OpenAI-like APIs.

Tool schema is translated from internal format (`input_schema`) to OpenAI function calling (`parameters`) when needed.

## Cost Tracking

Runtime cost is no longer hardcoded by model name.
Each call computes cost using tier pricing (`input_per_mtok`, `output_per_mtok`) and accumulates session total.

## Migration and Compatibility

- Existing code reading `settings.agent.provider/model/max_tokens/...` continues to work via compatibility accessors.
- Existing Anthropic-first flow works without config changes.
- New providers/tiers can be configured without code changes.

## Initial Implementation Scope

Implemented in this phase:

- config schema extension + legacy compatibility
- deterministic `ModelRouter`
- OpenAI-compatible client + factory support for `openrouter`
- runtime client pooling + tier selection + dynamic pricing
- tests for router/config and updated runtime integration tests

Deferred to next phase:

- richer routing heuristics using tool intent pre-classification
- dedicated summary-tier wiring in channels using `ModelTier.SUMMARY`
- docs/examples update in `config.yaml` and `.env.example`
