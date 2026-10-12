"""agy adapter: permission flag policy, `--output-format json` parsing (usage, denied_actions, errors), flat cost, unsupported effort."""
import json

from companyos import harness
from companyos.registry import Registry

REG = Registry()
A = REG.harnesses["agy"]
RESULT = {"status": "completed", "summary": "s"}
USAGE = {"input_tokens": 2199, "output_tokens": 117, "thinking_tokens": 5, "cache_read_tokens": 9708, "total_tokens": 2316}


def agy_json(response="Here:\n" + json.dumps(RESULT), status="SUCCESS", **extra):
    return json.dumps({"conversation_id": "c", "status": status, "response": response, "usage": USAGE, **extra})


def argv(caps):
    return harness.build_agy(A, "gemini-3.8-flash-high", harness.Request("sys", "task", ".", caps, None, 10), REG.result_schema)


def test_permission_flag_always_passed():
    for caps in ([], ["read_repository", "write_repository"], ["execute_command"]):
        assert "--dangerously-skip-permissions" in argv(caps)
    assert "--output-format" in argv([]) and "json" in argv([])


def test_result_cost_and_tokens_from_json():
    out, cost, err = harness.parse("agy", agy_json(), harness.price_for(A, "account:google-ai-pro", "gemini-3.8-flash-high"))
    assert out == RESULT and err == "" and cost == 0.0   # flat plan: exact zero, not an estimate
    assert harness.usage_of("agy", agy_json()) == {"inputTokens": 2199 + 9708, "outputTokens": 117, "cacheReadTokens": 9708, "reasoningTokens": 5}


def test_denied_tool_and_error_status_are_failures():
    out, _, err = harness.parse("agy", agy_json("", denied_actions=[{"action": "command", "display_name": "RunCommand"}]), None)
    assert out is None and "denied tool permission: command" in err
    out, _, err = harness.parse("agy", agy_json("", status="ERROR", error="invalid model selection"), None)
    assert out is None and "agy ERROR: invalid model" in err
    assert harness.parse("agy", "not json", None)[2] == "non-JSON output from agy"


def test_effort_is_unsupported_because_it_is_part_of_the_model_id():
    assert "effort" in harness.unsupported(A, "gemini-3.8-flash-high", "account:google-ai-pro", "high")
    assert harness.unsupported(A, "gemini-3.8-flash-high", "account:google-ai-pro", None) is None
