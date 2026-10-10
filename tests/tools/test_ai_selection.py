"""AI selection (protocols/ai): harness, provider, model and effort per agent, skill and task."""
import copy
import types

from companyos import harness, validate
from companyos.registry import Registry, merge_ai

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
