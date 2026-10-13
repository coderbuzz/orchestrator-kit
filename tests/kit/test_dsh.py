"""dsh harness: argv, JSON/usage parsing, cost, version pin, failure modes -> next mapped route. No model calls (scripted dsh in conftest)."""
import json

import pytest

from orchestrator_kit import harness
from orchestrator_kit.registry import Registry

REG = Registry()
D = REG.harnesses["dsh"]
RESULT = {"status": "completed", "summary": "s"}
AI = {"harness": "dsh", "provider": "deepseek", "model": "deepseek-flash", "effort": "low"}


def req(caps=(), timeout=30, cwd="."):
    return harness.Request("sys", "task", cwd, list(caps), None, timeout, provider="deepseek", effort="low")


def dsh_json(reply=None, status="completed", fail=None, **usage):
    u = {"prompt_tokens": 11000, "completion_tokens": 100, "prompt_cache_hit_tokens": 10000, "prompt_cache_miss_tokens": 1000, "reasoning_tokens": 40, **usage}
    return json.dumps({"status": status, "reply": json.dumps(RESULT) if reply is None else reply, "toolExecutions": [], "usage": u, "failReason": fail, "sessionId": "s1"}, indent=2)


def test_argv_modes_timeout_and_bash_removal():
    a = harness.build_dsh(D, "deepseek-flash", req(cwd="/work"), REG.result_schema)
    assert a[:4] == ["with-secrets", "deepseek", "--", "dsh"] and "--json" in a
    assert a[a.index("--mode") + 1] == "plan" and a[-2:] == ["--disallowed-tools", "bash"]
    assert a[a.index("--timeout") + 1] == "25" and a[a.index("-C") + 1] == "/work"
    prompt = a[a.index("-p") + 1]
    assert prompt.startswith("sys") and "`actions` is ONLY for actions" in prompt and "Valid example:" in prompt  # contract guide, as for zcode
    assert "-m" not in a and "-e" not in a   # selection args are appended by invoke
    assert harness.selection_args(D, "deepseek-flash", req()) == ["-m", "deepseek-flash", "-e", "low"]
    y = harness.build_dsh(D, "m", req(["execute_command"]), REG.result_schema)
    assert y[y.index("--mode") + 1] == "yolo" and "--disallowed-tools" not in y
    e = harness.build_dsh(D, "m", req(["write_repository"]), REG.result_schema)
    assert e[e.index("--mode") + 1] == "edit" and e[-2:] == ["--disallowed-tools", "bash"]


def test_usage_mapped_to_zcode_fields_and_priced_at_peak_tariff():
    price = harness.price_for(D, "deepseek", "deepseek-flash")
    assert price == {"in": 0.30, "cache_hit": 0.006, "out": 1.20}   # DeepSeek PEAK list price
    out, cost, err = harness.parse("dsh", dsh_json(), price)
    assert out == RESULT and err == ""
    assert harness.usage_of("dsh", dsh_json()) == {"inputTokens": 11000, "outputTokens": 100, "cacheReadTokens": 10000, "reasoningTokens": 40}
    assert cost == round(1000 * 0.30 / 1e6 + 10000 * 0.006 / 1e6 + 100 * 1.20 / 1e6, 6)


@pytest.mark.parametrize("stdout,code,expect", [
    (dsh_json(fail="boom", status="failed"), 1, "dsh failed: boom"),
    (dsh_json(status="interrupted"), 130, "dsh interrupted"),
    (dsh_json(status="waiting_for_user"), 1, "dsh waiting_for_user"),
    (dsh_json()[:300], 0, "non-JSON output from dsh"),          # truncated pipe (dsh <= 0.1.3)
    ("", 130, "exit 130"),
    ("", 1, "exit 1"),
    (dsh_json(), 1, "exit 1"),                                  # inconsistent: JSON says completed, process says failed
])
def test_failure_modes_are_failed_attempts_not_crashes(fake_dsh, stdout, code, expect):
    h, _ = fake_dsh(stdout, code)
    r = harness.invoke(h, "deepseek-flash", req(), REG.result_schema)
    assert not r.ok and expect in r.error and not r.error.startswith(harness.RETRYABLE)


def test_invoke_end_to_end_with_scripted_dsh(fake_dsh):
    h, argv = fake_dsh(dsh_json())
    r = harness.invoke(h, "deepseek-flash", req(), REG.result_schema)
    assert r.ok and r.output == RESULT and r.tokens["cacheReadTokens"] == 10000 and not r.cost_estimated and r.cost_usd > 0
    a = argv()
    assert a[a.index("-m") + 1] == "deepseek-flash" and a[a.index("-e") + 1] == "low" and "--json" in a


def test_output_over_64kib_is_valid_through_the_pipe(fake_dsh):
    h, _ = fake_dsh(dsh_json(reply=json.dumps({**RESULT, "summary": "x" * 200_000})))
    r = harness.invoke(h, "deepseek-flash", req(), REG.result_schema)
    assert r.ok and len(r.output["summary"]) == 200_000


def test_version_other_than_the_pin_is_a_failed_attempt_without_a_model_call(fake_dsh):
    h, argv = fake_dsh(dsh_json(), version="0.1.2")
    r = harness.invoke(h, "deepseek-flash", req(), REG.result_schema)
    assert not r.ok and "0.1.2" in r.error and "pinned 0.1.6" in r.error
    with pytest.raises(FileNotFoundError):
        argv()   # the scripted dsh never ran a prompt


def test_deepseek_is_only_offered_by_dsh_and_mapping_routes_use_it():
    assert "deepseek" not in {p["id"] for p in REG.harnesses["zcode"]["providers"]}
    assert not [m for m in REG.harnesses["zcode"]["models"] if m.startswith("deepseek")]
    for e in REG.mapping["entries"]:
        for ai in [e["ai"], *[f["ai"] for f in e["fallbacks"]]]:
            assert ai.get("provider") != "deepseek" or ai["harness"] == "dsh"


def test_truncated_dsh_goes_to_the_next_route_without_retry(fake_dsh):
    h, _ = fake_dsh(dsh_json()[:200])
    seen = []

    def fn(hd, m, rq, s):
        seen.append(hd["id"])
        return harness.invoke(h, m, rq, s) if hd["id"] == "dsh" else harness.CallResult(True, hd["id"], m, output=RESULT)
    res = harness.Router(REG, {}, check_installed=False).call_with_fallback("coding", req(), fn, ai=AI)
    assert seen[0] == "dsh" and seen[1] != "dsh" and [a.ok for a in res] == [False, True]   # truncation is not retried on the dsh route


def test_contract_violation_on_dsh_gets_the_corrective_retry_then_next_route(fake_dsh):
    h, _ = fake_dsh(dsh_json(reply=json.dumps({**RESULT, "actions": ["x"]})))
    calls = []

    def fn(hd, m, rq, s):
        calls.append(hd["id"])
        return harness.invoke(h, m, rq, s) if hd["id"] == "dsh" else harness.CallResult(True, hd["id"], m, output=RESULT)
    res = harness.Router(REG, {}, check_installed=False).call_with_fallback("coding", req(), fn, ai=AI)
    assert calls[:2] == ["dsh", "dsh"] and calls[2] != "dsh" and res[-1].ok


def test_descriptor_and_policies_validate():
    for pid in ("coding", "cheap", "research"):
        assert any(c["harness"] == "dsh" and c["provider"] == "deepseek" for c in REG.model_policies[pid]["candidates"])
