"""Deterministic policy engine: capabilities, approval levels, approver chain, delegation, state write scopes, spend thresholds.

A pack subclasses it for its own gates (company.CompanyPolicy) and maps its deterministic rules to code (company.ENFORCED).
The engine reads parameters of the policies `approval-levels` (thresholds) from the pack: those policy ids are part of the contract.
"""
from .registry import Registry

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

    def required_level(self, *, caps=(), spend=None, extra=0) -> int:
        lvls = [self.reg.capabilities[c].get("min_approval_level", 0) for c in caps] + [self.spend_level(spend), extra]
        return max(lvls)

    def needs_approval(self, agent_id: str, level: int) -> bool:
        return level > self.reg.agents[agent_id]["authority"]["max_approval_level"]

    def approver_for(self, agent_id: str, level: int) -> str:
        """First agent up the reports_to chain whose authority covers `level`; else the human (Board)."""
        human = self.reg.pack.human_id
        for a in self.reg.chain(agent_id):
            if a == human or level >= 4:
                return human
            if self.reg.agents[a]["authority"]["max_approval_level"] >= level:
                return a
        return human

    # delegation and state ownership
    def can_delegate(self, from_agent: str, to_agent: str) -> bool:
        return to_agent == from_agent or to_agent in self.reg.delegates(from_agent)

    def can_write(self, agent_id: str, scope: str) -> bool:
        return scope in self.reg.agents[agent_id]["memory_scope"]["writes"]
