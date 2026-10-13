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


def test_flat_plans_charge_zero_but_are_not_estimates():
    for prov in ("account:zai-start-plan", "account:zai-individual-coding-plan"):
        price = harness.price_for(Z, prov, "GLM-5.3-Flash")
        out, listed, err = harness.parse("zcode", zh_json(inputTokens=5000, outputTokens=500), price)
        assert out == RESULT and listed is None, "no list price in the descriptor: the equivalent stays unknown, tokens are kept"
        assert harness.billing(price, listed) == (0.0, None), "charged 0, not None (None would apply the config estimate)"


def test_flat_plan_with_list_prices_keeps_the_equivalent_and_dsh_is_charged():
    flat = {"flat": True, **DS}
    listed = harness.token_cost(flat, {"inputTokens": 1000, "outputTokens": 100})
    assert listed == round(1000 * 0.30 / 1e6 + 100 * 1.20 / 1e6, 6) and harness.billing(flat, listed) == (0.0, listed)
    assert harness.billing(DS, listed) == (listed, listed), "pay-as-you-go (DeepSeek on dsh) counts against the budget"


def test_claude_code_on_a_flat_plan_charges_zero_and_keeps_the_cli_cost():
    out, listed, err = harness.parse("claude-code", json.dumps({"subtype": "success", "is_error": False, "total_cost_usd": 0.42,
                                                                "structured_output": RESULT}))
    assert out == RESULT and listed == 0.42
    assert harness.billing({"flat": True}, listed) == (0.0, 0.42) and harness.billing(None, listed) == (0.42, 0.42)


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


CLAUDE_OUT = json.dumps({"subtype": "success", "is_error": False, "total_cost_usd": 0.0024, "structured_output": RESULT,
                         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 19166, "cache_read_input_tokens": 11082, "output_tokens": 182}})


def test_claude_code_default_provider_is_flat_priced_from_tokens():
    C = {**REG.harnesses["claude-code"], "pricing_usd_per_mtok": {"anthropic/claude-haiku-5-5": {"flat": True, "in": 0.10, "cache_write": 0.125, "cache_hit": 0.01, "out": 0.50}}}
    price = harness.price_for(C, None, "claude-haiku-5-5")  # no provider on the selection: the harness's only provider
    assert price and price.get("flat")
    out, listed, err = harness.parse("claude-code", CLAUDE_OUT, price)
    u = harness.usage_of("claude-code", CLAUDE_OUT)
    assert u == {"inputTokens": 2 + 19166 + 11082, "cacheWriteTokens": 19166, "cacheReadTokens": 11082, "outputTokens": 182}
    assert listed == harness.token_cost(price, u) and listed != 0.0024, "equivalent comes from tokens x descriptor tariff, not the CLI figure"
    assert harness.billing(price, listed) == (0.0, listed)


def test_router_records_the_default_provider():
    r = harness.Router(REG, {}, check_installed=False)
    rt = r.route("default", {"harness": "claude-code", "model": "claude-haiku-5-5"})[0]
    assert rt.provider == "anthropic"
