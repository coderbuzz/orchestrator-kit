"""Deterministic policy engine: capabilities, approval levels, approver chain, delegation, state ownership, budgets.

Every `enforcement: deterministic` rule in policies/ is mapped in ENFORCED to the code that enforces it;
tests/policies/test_policy_engine.py fails if a deterministic rule has no entry or an entry names code that does not exist.
"""
from .registry import ORCHESTRATOR_ONLY_SECTIONS, Registry

ENFORCED = {
    "approval-levels/autonomy-default": "policy.required_level (level 0 needs nothing)",
    "approval-levels/no-self-escalation": "tools.Company.resolve_approval",
    "approval-levels/non-blocking": "tools.Company._request_approval (only the requesting task waits)",
    "approval-levels/spend-thresholds": "policy.spend_level",
    "decision-rights/ceo-decides": "tools.Company._record_decision (decision tasks, options checked by verify.verify_task)",
    "decision-rights/board-reserved": "policy.effect_level + tools.Company.product_stage",
    "decision-rights/delegation-chain": "policy.can_delegate in tools.Company.add_task",
    "decision-rights/approver-chain": "policy.approver_for",
    "security-baseline/no-secrets-in-git": "store.redact + tests/policies/test_no_secrets.py",
    "security-baseline/least-privilege": "policy.check_capability + tools.Company._filter_unauthorized + harness tool mapping",
    "data-protection/redact-secrets": "store.redact on every persisted record",
    "data-protection/isolated-workspaces": "tools.Company._workspace + claude --safe-mode (harness descriptor)",
    "spending/budget-checked": "tools.Company._retry_or_fail (over-budget task is not retried)",
    "spending/spend-above-limit-needs-approval": "policy.spend_level",
    "budget-control/run-budget-required": "tools.Company.add_task (default budget = spending cap)",
    "budget-control/track-cost": "tools.Company.dispatch -> usage.jsonl",
    "budget-control/daily-cap": "tools.Company.dispatch (daily-cap refusal)",
    "autonomy-limits/bounded-retries": "tools.Company.start / _retry_or_fail",
    "autonomy-limits/bounded-fallback": "harness.Router.call_with_fallback",
    "audit-trail/audit-record": "tools.Company.audit",
    "audit-trail/append-only": "store.append",
    "evidence-and-decisions/evidence-over-claims": "verify.verify_task in tools.Company._process",
    "deployment-gates/staging-first": "policy.deployment_gate",
    "deployment-gates/tests-green": "policy.deployment_gate",
    "deployment-gates/production-approval": "capability deploy_production min_approval_level",
    "incident-management/deploy-failure-incident": "tools.Company._process (irreversible failure -> FAILED + Board inbox)",
    "incident-management/no-blind-retry-of-deploys": "tools.Company._execute_actions / _process",
    "external-communication/customer-actions-gated": "capability min_approval_level for send_email/publish_content",
    "board-reporting/summarize": "board.snapshot (schema-validated)",
    "board-reporting/level-3-notify": "tools.Company._process (level-3 notice) / _request_approval (Board inbox)",
}


class Policy:
    def __init__(self, reg: Registry, configured: set | None = None):
        self.reg = reg
        self.configured = configured or set()  # capabilities with an adapter supplied at runtime

    # capabilities
    def check_capability(self, agent_id: str, cap: str) -> str | None:
        """None if allowed, else the reason."""
        if cap not in self.reg.capabilities:
            return f"unknown capability '{cap}'"
        if cap not in self.reg.agents[agent_id]["tools"]:
            return f"agent '{agent_id}' does not hold capability '{cap}'"
        return None

    def available(self, cap: str) -> bool:
        return self.reg.capabilities[cap].get("adapter") is not None or cap in self.configured

    # approval levels
    def spend_level(self, usd: float | None) -> int:
        t = self.reg.policy_params("approval-levels").get("thresholds") or {}
        if not usd:
            return 0
        for lvl in (4, 3, 2, 1):  # spend above level_N_usd needs level N
            limit = t.get(f"level_{lvl}_usd")
            if limit is not None and usd > limit:
                return lvl
        return 0

    def effect_level(self, effect: dict, product_stage: str | None) -> int:
        """Board-reserved effects (decision-rights) -> 4."""
        live = self.reg.product_lifecycle.get("live_states", [])
        if effect.get("product_transition") in ("PAUSED", "DEPRECATED", "STOPPED") and product_stage in live:
            return 4
        if effect.get("apply_to_target") in ("stop", "pause") and product_stage in live:
            return 4
        return 0

    def required_level(self, *, caps=(), spend=None, extra=0) -> int:
        lvls = [self.reg.capabilities[c].get("min_approval_level", 0) for c in caps] + [self.spend_level(spend), extra]
        return max(lvls)

    def needs_approval(self, agent_id: str, level: int) -> bool:
        return level > self.reg.agents[agent_id]["authority"]["max_approval_level"]

    def approver_for(self, agent_id: str, level: int) -> str:
        """First agent up the reports_to chain whose authority covers `level`; else the Board."""
        for a in self.reg.chain(agent_id):
            if a == "board" or level >= 4:
                return "board"
            if self.reg.agents[a]["authority"]["max_approval_level"] >= level:
                return a
        return "board"

    # delegation and state ownership
    def can_delegate(self, from_agent: str, to_agent: str) -> bool:
        return to_agent == from_agent or to_agent in self.reg.delegates(from_agent)

    def can_write(self, agent_id: str, scope: str) -> bool:
        kind, _, section = scope.partition("/")
        if kind == "company" and section in ORCHESTRATOR_ONLY_SECTIONS:
            return False
        return scope in self.reg.agents[agent_id]["memory_scope"]["writes"]

    # production gates
    def deployment_gate(self, target: str, run_artifacts: list[dict]) -> str | None:
        """Production deploy needs an accepted staging deployment and a passing test_result in the same run."""
        if target != "production":
            return None
        ok = lambda t, pred=lambda a: True: any(a["type"] == t and a["status"] == "accepted" and pred(a) for a in run_artifacts)  # noqa: E731
        if not ok("deployment", lambda a: a["evidence"].get("environment") == "staging"):
            return "deployment-gates/staging-first: no accepted staging deployment in this run"
        if not ok("test_result"):
            return "deployment-gates/tests-green: no accepted test_result in this run"
        return None
