# AGENTS.md: operating context for coding agents in this repo

## What this is
orchestrator-kit: the framework under company repos (packs) such as company-os (QUMATIC). Read README.md first.
Package `orchestrator_kit/`: `validate` (schemas + cross-references), `pack` (manifest), `registry` (definitions, versions,
AI resolution), `harness` (descriptors, builders, parsers, Router), `runtime` (guardrails), `store` (atomic files, ledger,
redaction), `verify` (evidence), `policy`, `mcp` (stdio server + core tools), `plugin` (generator), `hook` (stdlib-only guard),
`scaffold` (new-company, hire, new-project, org), `profiling` (battery, match), `cli`. Data: `schemas/` (core contracts),
`library/` (the role library; itself a valid pack).

## Rules
- Nothing organization-specific in the kit: no company names, no human id other than the configured one, no VM habits
  (secret wrappers, paths). `tests/kit/test_scaffold.py` scans the library for such words.
- The hook and `cli.py` stay stdlib-only at import time (tests check it): they run on every tool call.
- Never fake an integration: a capability without an adapter is reported unavailable; an unregistered evidence check fails.
- A mapping is never applied by code: `match` writes proposals; humans approve.
- Change a library role: bump its `version`; companies that hired it keep their copy until they re-hire.

## Backward compatibility
Additive schema changes keep the major; breaking changes bump the schema `$id` major and add a note in CHANGELOG.md.
While the kit is 0.x a minor release may break, with a CHANGELOG note; packs pin a tag.
Schema `$id`s keep the `company-os/...` namespace (stored nowhere; renaming them is a breaking change for no gain).

## Test
`uv run python -m pytest` (the library is the default company; `examples/review-pipeline` is the second pack).

## Release
Tag `vX.Y.Z` on main; packs depend on it with `[tool.uv.sources] orchestrator-kit = { git = "...", tag = "vX.Y.Z" }`.
Do not push unless asked.
