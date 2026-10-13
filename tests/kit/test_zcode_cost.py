"""zh --json: usage tokens x pricing_usd_per_mtok -> cost (flat plans 0, unpriced routes keep the estimate)."""
import json

from orchestrator_kit import harness
from orchestrator_kit.registry import Registry

REG = Registry()
DS = {"in": 0.30, "cache_hit": 0.006, "out": 1.20}   # the DeepSeek tariff; the zcode parser is generic, the price is passed in
Z = REG.harnesses["zcode"]
RESULT = {"status": "completed", "summary": "s"}


def zh_json(**usage):
    return json.dumps({"sessionId": "sess_x", "response": "Here:\n" + json.dumps(RESULT), "usage": {"source": "provider", **usage}}, indent=2)


def test_json_flag_in_invocation_and_response_unwrapped():
    assert "--json" in Z["invocation"]["non_interactive_args"]
    out, cost, err = harness.parse("zcode", zh_json(inputTokens=1000, outputTokens=100), DS)
    assert out == RESULT and err == ""
    assert cost == round(1000 * 0.30 / 1e6 + 100 * 1.20 / 1e6, 6)


def test_cache_read_tokens_priced_at_the_hit_rate():
    p = DS
    assert harness.token_cost(p, {"inputTokens": 11000, "cacheReadTokens": 10000, "outputTokens": 0}) == round(1000 * 0.30 / 1e6 + 10000 * 0.006 / 1e6, 6)


def test_flat_plans_cost_zero_but_are_not_estimates():
    for prov in ("account:zai-start-plan", "account:zai-individual-coding-plan"):
        out, cost, err = harness.parse("zcode", zh_json(inputTokens=5000, outputTokens=500), harness.price_for(Z, prov, "GLM-5.3-Flash"))
        assert cost == 0.0 and out == RESULT


def test_unpriced_route_and_plain_stdout_fall_back():
    assert harness.price_for(Z, "anthropic", "claude-opus-5") is None
    out, cost, err = harness.parse("zcode", zh_json(inputTokens=1), None)
    assert out == RESULT and cost is None   # caller applies the config estimate
    out, cost, err = harness.parse("zcode", "text " + json.dumps(RESULT), None)  # older zh without --json
    assert out == RESULT and cost is None


def test_router_keeps_estimate_for_unpriced_and_tokens_in_result():
    r = harness.Router(REG, {}, check_installed=False)
    res = r.call_with_fallback("default", harness.Request("", "", ".", [], None, 10),
                               lambda h, m, rq, s: harness.CallResult(True, h["id"], m, output=RESULT, cost_estimated=True, tokens={"inputTokens": 9}),
                               which=r.route("default")[:1])
    assert res[0].tokens == {"inputTokens": 9}
