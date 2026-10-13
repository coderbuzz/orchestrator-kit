"""AI selection (protocols/ai): harness, provider, model and effort per agent, skill and task."""
import copy
import types

from orchestrator_kit import harness, validate
from orchestrator_kit.registry import Registry, merge_ai

REG = Registry()


def test_merge_precedence_and_harness_switch():
    assert merge_ai({"effort": "low"}, {"harness": "claude-code", "model": "sonnet"}, None, {"model": "opus"}, {"effort": "high"}) == \
        {"harness": "claude-code", "model": "opus", "effort": "high"}
    # a higher layer that switches harness drops the lower layer's model/provider, keeps effort
    assert merge_ai({"harness": "claude-code", "model": "opus", "effort": "max"}, {"harness": "zcode"}) == {"harness": "zcode", "effort": "max"}


def test_claude_args_carry_model_and_effort():
    o = types.SimpleNamespace(reg=REG)
    h = o.reg.harnesses["claude-code"]
    args = harness.selection_args(h, "opus", harness.Request("", "", ".", [], None, 10, effort="xhigh"))
    assert args == ["--model", "opus", "--effort", "xhigh"]


def test_unsupported_selection_is_visible_and_falls_back():
    o = types.SimpleNamespace(reg=REG)
    r = harness.Router(o.reg, {}, check_installed=False)
    seen = []
    ok = lambda h, m, req, s: seen.append((h["id"], m, req.effort)) or harness.CallResult(True, h["id"], m, output={"status": "completed", "summary": "s"})  # noqa: E731
    res = r.call_with_fallback("default", harness.Request("", "", ".", [], None, 10), ok,
                               ai={"harness": "claude-code", "model": "opus", "provider": "zai", "effort": "high"})
    assert not res[0].ok and res[0].harness == "claude-code" and "provider 'zai'" in res[0].error, "an unhonourable selection is never dropped silently"
    assert seen == [("claude-code", "sonnet", "high")] and res[-1].ok
    assert r.health == {"claude-code": {"failures": 0, "since": 0}}, "an unsupported selection does not trip the circuit breaker"
    z = o.reg.harnesses["zcode"]
    assert harness.selection_args(z, "deepseek-v4", harness.Request("", "", ".", [], None, 10, provider="deepseek", effort="high")) == \
        ["--model", "deepseek/deepseek-v4", "--effort", "high"]
    assert harness.selection_args(z, "configured", harness.Request("", "", ".", [], None, 10, effort="low")) == ["--effort", "low"]
    assert harness.unsupported(o.reg.harnesses["claude-code"], "opus", "zai", None) == "claude-code does not offer provider 'zai'"
    assert "does not accept effort 'disabled'" in harness.unsupported(o.reg.harnesses["claude-code"], "opus", None, "disabled")


def test_zh_built_in_models_and_opus_only_decisions():
    z = REG.harnesses["zcode"]
    glm = {"provider": "account:zai-individual-coding-plan", "model": "GLM-5.3"}
    assert harness.selection_args(z, glm["model"], harness.Request("", "", ".", [], None, 10, provider=glm["provider"], effort="high")) == \
        ["--model", "account:zai-individual-coding-plan/GLM-5.3", "--effort", "high"]
    assert harness.unsupported(z, "GLM-5.3", glm["provider"], "high") is None
    assert validate._candidate_checks(REG) == []
    # policies list zh GLM built-in models (DeepSeek only via dsh, last); strategic is Opus only (a decision is blocked, not downgraded)
    models = {p: [(c["harness"], c["model"]) for c in m["candidates"]] for p, m in REG.model_policies.items()}
    assert models["strategic"] == [("claude-code", "opus")]   # Claude plan only; zh has no Anthropic route (Board 2026-10-12)
    assert ("zcode", "GLM-5.3-Flash") in models["cheap"] and ("zcode", "GLM-5.3") in models["coding"]
    assert not any(m == "configured" for c in models.values() for _, m in c)


def test_candidate_with_unknown_provider_or_model_is_rejected():
    reg = copy.copy(REG)
    bad = {**REG.model_policies["coding"], "candidates": [{"harness": "zcode", "model": "GLM-9", "provider": "zai"},
                                                          {"harness": "zcode", "model": "GLM-5.3"}]}
    reg.model_policies = {**REG.model_policies, "coding": bad}
    errs = validate._candidate_checks(reg)
    assert "model policy coding: harness zcode does not offer provider 'zai'" in errs
    assert "model policy coding: harness zcode has no verified model 'GLM-9'" in errs
    assert "model policy coding: zcode candidate 'GLM-5.3' needs a provider" in errs


def test_validator_rejects_selection_a_harness_cannot_honour():
    o = types.SimpleNamespace(reg=REG)
    reg = copy.copy(o.reg)
    reg.agents = {**o.reg.agents, "cto": {**o.reg.agents["cto"], "ai": {"harness": "zcode", "model": "glm-5.3", "provider": "zai"}},
                  "cfo": {**o.reg.agents["cfo"], "ai": {"harness": "zcode", "model": "deepseek-v4"}}}
    reg.config = {**o.reg.config, "models": {**o.reg.config["models"], "default_ai": {"model": "opus"}}}
    errs = validate._ai_checks(reg)
    assert "agent cto: zcode does not offer provider 'zai'" in errs
    assert "agent cfo: zcode needs a provider with model 'deepseek-v4'" in errs
    assert "config models.default_ai: ai.model/provider need ai.harness" in errs
    assert validate._ai_checks(o.reg) == []


def test_claude_gets_a_self_contained_result_schema():
    """Regression (found by a real profiling run 2026-10-13): claude --json-schema failed every call on the `../ai/1` reference."""
    import json
    from jsonschema import Draft202012Validator
    reg = Registry()
    argv = harness.build_claude(reg.harnesses["claude-code"], "haiku", harness.Request("s", "p", "/tmp", [], 0.1, 60), reg.result_schema)
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert "../ai/1" not in json.dumps(schema) and "$id" not in schema and "$schema" not in schema
    Draft202012Validator.check_schema(schema)
    plan_item = {"key": "a", "agent": "cto", "objective": "o", "acceptance_criteria": ["c"], "ai": {"harness": "zcode", "effort": "low"}}
    ok = {"status": "completed", "summary": "s", "artifacts": [], "plan": [plan_item]}
    assert not list(Draft202012Validator(schema).iter_errors(ok))
    assert list(Draft202012Validator(schema).iter_errors({**ok, "plan": [{**plan_item, "ai": {"effort": "huge"}}]})), "the inlined AI schema still applies"


def test_inactive_provider_is_skipped_without_a_call():
    reg = copy.copy(REG)
    reg.config = copy.deepcopy(REG.config)
    for h in reg.config["models"]["harnesses"]:
        if h["id"] == "zcode":
            h.update(enabled=True, inactive_providers=["account:zai-start-plan"])
    fb = [{"harness": "zcode", "provider": "account:zai-individual-coding-plan", "model": "GLM-5.3-Flash"}]
    ai = {"harness": "zcode", "provider": "account:zai-start-plan", "model": "GLM-5.3-Flash"}
    routes = harness.Router(reg, {}, check_installed=False).route("default", ai, fb)
    assert routes[0].provider == "account:zai-individual-coding-plan" and all(r.provider != "account:zai-start-plan" for r in routes)
