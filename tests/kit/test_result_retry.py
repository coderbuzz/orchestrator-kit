"""zcode contract guide, one corrective retry, and approved-mapping fallbacks in the Router."""
import json

from orchestrator_kit import harness
from orchestrator_kit.registry import Registry
from orchestrator_kit.validate import validate_instance

REG = Registry()
OK = {"status": "completed", "summary": "s"}


def req():
    return harness.Request("sys", "task", ".", [], None, 10)


def test_zcode_prompt_carries_a_valid_example():
    argv = harness.build_zcode(REG.harnesses["zcode"], "m", req(), REG.result_schema)
    prompt = argv[argv.index("-p") + 1]
    assert "`actions` is ONLY for actions" in prompt
    from orchestrator_kit.verify import check_evidence
    examples = [json.loads(line) for line in harness.CONTRACT_GUIDE.splitlines() if line.startswith("{")]
    assert len(examples) == 2
    for ex in examples:
        assert validate_instance("agent-result", ex) == []
        art = ex["artifacts"][0]
        assert check_evidence(REG.evidence_rules, art["type"], art["evidence"]) == []


def _router():
    return harness.Router(REG, {}, check_installed=False)


def test_empty_answer_gets_one_corrective_retry_on_the_same_route():
    seen = []

    def fn(h, m, r, s):
        seen.append(r.prompt)
        return harness.CallResult(True, h["id"], m, output=OK) if len(seen) == 2 else harness.CallResult(False, h["id"], m, error="no agent-result JSON in output")
    res = _router().call_with_fallback("default", req(), fn, which=_router().route("default")[:1])
    assert [a.ok for a in res] == [False, True] and "previous answer was rejected" in seen[1] and seen[0] == "task"


def test_contract_violation_retried_once_then_next_route_other_errors_not_retried():
    calls = []

    def bad(h, m, r, s):
        calls.append((h["id"], m))
        return harness.CallResult(True, h["id"], m, output={"status": "completed", "summary": "s", "actions": ["x"]})
    r = _router()
    routes = r.route("default")[:2]
    res = r.call_with_fallback("default", req(), bad, which=routes)
    assert len(res) == 4 and not res[-1].ok and calls[0] == calls[1] and calls[2] == calls[3] != calls[0]

    calls.clear()
    res = r.call_with_fallback("default", req(), lambda h, m, rq, s: calls.append(1) or harness.CallResult(False, h["id"], m, error="timeout after 10s"), which=routes)
    assert len(calls) == 2, "a timeout is not retried on the same route"


def test_a_route_gets_the_whole_ceiling_and_a_stopped_run_is_not_rerouted():
    r = _router()
    routes = r.route("default")[:2]
    assert len(routes) == 2
    seen = []
    big = harness.Request("sys", "task", ".", [], None, 10800)
    r.call_with_fallback("default", big, lambda h, m, rq, s: seen.append(rq.timeout_seconds) or harness.CallResult(False, h["id"], m, error="exit 1: provider error"), which=routes)
    assert len(seen) == 2 and seen[0] >= 10799, "a real failure moves on, and no route is cut to a fraction of the ceiling"
    seen.clear()
    r = _router()
    stuck = lambda h, m, rq, s: seen.append(1) or harness.CallResult(False, h["id"], m, error="stopped by supervisor (loop)", stopped="loop")  # noqa: E731
    out = r.call_with_fallback("default", big, stuck, which=routes)
    assert len(seen) == 1 and out[-1].stopped == "loop", "stuck/loop: stop and report, never another route on its own"
    assert not r.health.get(routes[0].hdef["id"], {}).get("failures"), "a stopped run does not trip the circuit breaker"
