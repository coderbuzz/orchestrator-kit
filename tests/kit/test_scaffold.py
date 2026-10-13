"""Generators: the role library, a new company, hiring with dependencies and rollback, projects hiring from the catalog."""
import json
import subprocess
import sys

import pytest
import yaml

from orchestrator_kit import scaffold, validate
from orchestrator_kit.registry import Registry
from orchestrator_kit.runtime import Refused


@pytest.fixture
def acme(tmp_path):
    scaffold.new_company(tmp_path / "acme", "acme", "Acme Labs", mission="Run small SaaS products.")
    return tmp_path / "acme"


def test_the_role_library_is_a_valid_pack_without_qumatic_words():
    assert validate.check(scaffold.LIB) == []
    text = "\n".join(p.read_text() for p in scaffold.LIB.rglob("*") if p.is_file())
    assert not [w for w in ("QUMATIC", "companyos", "with-secrets", "Nalar") if w in text]
    assert {"ceo", "cto", "fullstack-developer", "cmo", "sales-development", "customer-success"} <= {r["id"] for r in scaffold.roles()}


def test_new_company_has_its_orchestrator_and_a_working_plugin(acme):
    reg = Registry(acme)
    assert list(reg.agents) == ["ceo"] and reg.agents["ceo"]["authority"]["reports_to"] == "board"
    assert reg.agents["ceo"]["authority"]["delegates_to"] == []
    assert scaffold.write_plugin(acme, check=True) == [], "new-company writes plugin/"
    mcp = json.loads((acme / "plugin/.mcp.json").read_text())["company"]
    assert mcp == {"command": "orchestrator-kit", "args": ["mcp", str(acme.resolve())]}
    hook = json.loads((acme / "plugin/hooks/hooks.json").read_text())["hooks"]["PreToolUse"][0]
    assert hook["matcher"].startswith("mcp__plugin_acme_company__board_.*") and hook["hooks"][0]["command"] == "orchestrator-kit hook .company board_"
    skill = (acme / "plugin/skills/ceo/SKILL.md").read_text()
    assert "`board_staff`" in skill and "roster" in skill and "Run small SaaS products" not in skill
    assert "Run small SaaS products." in (acme / "company/mission/mission.md").read_text()
    assert json.loads((acme / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["source"] == "./plugin"


def test_hiring_copies_what_the_role_needs_and_builds_the_org_chart(acme):
    changed = scaffold.hire(acme, "cto")
    assert "agents/executives/cto.agent.yaml" in changed and "agents/executives/ceo.agent.yaml" in changed
    scaffold.hire(acme, "fullstack-developer")
    scaffold.hire(acme, "sales-development")            # its boss (cro) is not hired: it reports to the orchestrator
    reg = Registry(acme)
    assert reg.agents["fullstack-developer"]["authority"]["reports_to"] == "cto"
    assert reg.agents["sales-development"]["authority"]["reports_to"] == "ceo"
    assert set(reg.agents["ceo"]["authority"]["delegates_to"]) == {"cto", "sales-development"}
    assert reg.enabled_agents == {"ceo", "cto", "fullstack-developer", "sales-development"}
    assert not Registry(acme).capabilities["create_sales_lead"]["adapter"], "no adapter in the library: reported unavailable, never faked"
    assert validate.check(acme) == []
    org = scaffold.org(acme)
    assert "  - fullstack-developer" in org and "- cto" in org
    scaffold.hire(acme, "qa-engineer", as_id="qa-2")
    assert Registry(acme).agents["qa-2"]["prompt_file"].endswith("qa-2.prompt.md")


def test_hiring_refuses_and_rolls_back(acme, monkeypatch):
    with pytest.raises(Refused, match="no role"):
        scaffold.hire(acme, "astronaut")
    with pytest.raises(Refused, match="already has an orchestrator"):
        scaffold.hire(acme, "ceo", as_id="ceo-2")
    scaffold.hire(acme, "cto")
    with pytest.raises(Refused, match="already has an agent 'cto'"):
        scaffold.hire(acme, "cto")
    before = {p: p.read_text() for p in acme.rglob("*") if p.is_file()}
    monkeypatch.setattr(validate, "check", lambda root: ["broken on purpose"])
    with pytest.raises(Refused, match="nothing was changed"):
        scaffold.hire(acme, "fullstack-developer")
    assert {p: p.read_text() for p in acme.rglob("*") if p.is_file()} == before


def test_project_hires_its_team_from_the_company_catalog(acme, tmp_path):
    scaffold.hire(acme, "cto")
    cfg = scaffold.new_project(tmp_path / "ws/shop", acme, "Web shop", 3, goal="Launch the MVP", roster=["cto"])
    assert cfg["roster"] == ["cto"] and yaml.safe_load((tmp_path / "ws/shop/company.yaml").read_text())["goal"] == "Launch the MVP"
    with pytest.raises(Refused, match="hire into the company first"):
        scaffold.new_project(tmp_path / "ws/crm", acme, "CRM", 2, roster=["sales-development"])
    assert not (tmp_path / "ws/crm").exists()


def test_cli_end_to_end(tmp_path):
    run = lambda *a, **kw: subprocess.run([sys.executable, "-m", "orchestrator_kit.cli", *a], capture_output=True, text=True, **kw)  # noqa: E731
    co = tmp_path / "co"
    assert run("new-company", str(co), "--name", "co", "--title", "Co").returncode == 0
    assert run("hire", "cmo", "--pack", str(co)).returncode == 0
    assert run("validate", "--pack", str(co)).stdout.strip() == "OK: all definitions valid"
    assert "cmo" in run("org", "--pack", str(co)).stdout
    assert run("plugin", "--pack", str(co), "--check").returncode == 1, "hiring changes the plugin until it is regenerated"
    assert run("plugin", "--pack", str(co)).returncode == 0 and run("plugin", "--pack", str(co), "--check").returncode == 0
    bad = run("hire", "nobody", "--pack", str(co))
    assert bad.returncode == 1 and "refused: no role 'nobody'" in bad.stderr
    hook = run("hook", ".company", "board_", input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat x/.company/state/a"}}))
    assert json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    loaded = subprocess.run([sys.executable, "-c", "import sys, orchestrator_kit.cli; print(sorted({'yaml', 'jsonschema'} & set(sys.modules)))"],
                            capture_output=True, text=True).stdout.strip()
    assert loaded == "[]", "the CLI module stays stdlib-only at import: the hook runs through it"
