"""Load every definition from the repo once and resolve references (versions, config, policy parameters).

The registry is read-only: Git is the source of truth for behavior; runtime state lives in companyos.store.
"""
import re
from pathlib import Path

from . import validate as v

ORCHESTRATOR_ONLY_SECTIONS = {"products", "decisions", "policies", "as_of", "schema_version"}


def parse_version(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split("."))


def version_matches(version: str, spec: str | None) -> bool:
    """spec: None/'' = any, '^1' = same major, '1.2' = prefix, '1.2.0' = exact."""
    if not spec:
        return True
    if spec.startswith("^"):
        return parse_version(version)[0] == int(spec[1:].split(".")[0])
    want = parse_version(spec)
    return parse_version(version)[: len(want)] == want


def split_ref(ref) -> tuple[str, str | None]:
    """{'id','version'} | 'id' | 'id@^1' -> (id, spec)."""
    if isinstance(ref, dict):
        return ref["id"], ref.get("version")
    sid, _, spec = ref.partition("@")
    return sid, spec or None


def merge_ai(*layers: dict | None) -> dict:
    """Merge AI selections, lowest precedence first. A layer naming another harness drops the provider and model
    inherited from below (they belong to the old harness); effort carries over."""
    out: dict = {}
    for layer in layers:
        if not layer:
            continue
        if layer.get("harness") and layer["harness"] != out.get("harness"):
            out.pop("provider", None)
            out.pop("model", None)
        out.update(layer)
    return out


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or v.ROOT)
        found = v.discover(self.root)
        defs = lambda kind: [d for _, d in found.get(kind, [])]  # noqa: E731
        self.agents = {d["id"]: d for d in defs(".agent.yaml")}
        self.model_policies = {d["id"]: d for d in defs(".model-policy.yaml")}
        self.harnesses = {d["id"]: d for d in defs(".harness.yaml")}
        self.capabilities = {c["id"]: c for d in defs(".capabilities.yaml") for c in d["capabilities"]}
        self._skills = self._by_version(defs(".skill.yaml"))
        self._policies = self._by_version(defs(".policy.yaml"))
        self.config = {}
        for kind in ("company", "models", "agents", "policies", "environments"):
            items = {p.name: d for p, d in found.get(f"config:{kind}", [])}
            self.config[kind] = items.get("local.yaml") or items.get("example.yaml") or {}
        self.task_lifecycle = v.load(self.root / "protocols/tasks/lifecycle.yaml")
        self.product_lifecycle = v.load(self.root / "protocols/products/lifecycle.yaml")
        self.evidence_rules = v.load(self.root / "protocols/artifacts/evidence-requirements.yaml")
        self.result_schema = v.load(self.root / "protocols/results/agent-result.schema.json")
        self.state_schema = v.load(self.root / "company/state.schema.json")
        self.preamble = (self.root / "agents/shared/company-preamble.md").read_text()

    @staticmethod
    def _by_version(items):
        out: dict[str, list] = {}
        for d in items:
            out.setdefault(d["id"], []).append(d)
        for lst in out.values():
            lst.sort(key=lambda d: parse_version(d["version"]), reverse=True)
        return out

    @staticmethod
    def _resolve(table, ref):
        rid, spec = split_ref(ref)
        for d in table.get(rid, []):
            if version_matches(d["version"], spec):
                return d
        raise KeyError(f"no definition matches {rid}@{spec or 'latest'}")

    def skill(self, ref) -> dict:
        return self._resolve(self._skills, ref)

    def policy(self, ref) -> dict:
        return self._resolve(self._policies, ref)

    def has_skill(self, ref) -> bool:
        try:
            self.skill(ref)
            return True
        except KeyError:
            return False

    def agent_skill(self, agent_id: str, skill_id: str) -> dict:
        """Resolve a skill through the agent's own reference (its pinned version)."""
        sid, spec = split_ref(skill_id)
        for ref in self.agents[agent_id]["skills"]:
            if ref["id"] == sid:
                return self.skill({"id": sid, "version": spec or ref.get("version")})
        raise KeyError(f"agent {agent_id} does not hold skill {sid}")

    def policy_params(self, policy_id: str) -> dict:
        base = dict(self.policy(policy_id).get("parameters") or {})
        base.update((self.config["policies"].get("parameters") or {}).get(policy_id) or {})
        return base

    @property
    def enabled_agents(self) -> set[str]:
        return set(self.config["agents"].get("enabled") or [])

    def enabled_harnesses(self) -> dict[str, dict]:
        return {h["id"]: h for h in self.config["models"].get("harnesses", []) if h.get("enabled")}

    def model_policy_for(self, agent_id: str, skill: dict | None, override: str | None = None) -> str:
        agent_override = ((self.config["agents"].get("overrides") or {}).get(agent_id) or {}).get("model_policy")
        return override or (skill or {}).get("model_policy") or agent_override or self.agents[agent_id]["model_policy"]

    def ai_for(self, agent_id: str, skill: dict | None, task_ai: dict | None = None) -> dict:
        """Resolved AI selection: task > skill > config override > agent > config default (see protocols/ai)."""
        override = ((self.config["agents"].get("overrides") or {}).get(agent_id) or {}).get("ai")
        layers = [self.config["models"].get("default_ai"), self.agents[agent_id].get("ai"), override,
                  (skill or {}).get("ai"), task_ai]
        return merge_ai(*layers)

    def _abs(self, p: str) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def state_dir(self) -> Path:
        return self._abs((self.config["company"].get("state_store") or {}).get("path") or "./state")

    @property
    def workspaces_dir(self) -> Path:
        return self._abs(self.config["company"].get("workspaces_path") or "./state/workspaces")

    def chain(self, agent_id: str) -> list[str]:
        """reports_to chain upward, excluding the agent itself, ending with 'board'."""
        out, cur = [], agent_id
        while cur != "board" and len(out) < 20:
            cur = self.agents[cur]["authority"]["reports_to"]
            out.append(cur)
        return out

    def delegates(self, agent_id: str) -> set[str]:
        """Transitive delegates_to closure."""
        seen, todo = set(), [agent_id]
        while todo:
            for d in self.agents.get(todo.pop(), {}).get("authority", {}).get("delegates_to", []):
                if d not in seen:
                    seen.add(d)
                    todo.append(d)
        return seen

    def agents_with_skill(self, skill_id: str) -> list[str]:
        return sorted(a for a, d in self.agents.items() if any(s["id"] == skill_id for s in d["skills"]))


SECTION_RE = re.compile(r"^(company|department)/[a-z_]+$")
