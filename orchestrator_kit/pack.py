"""The pack manifest: what the framework must know about an organization beyond its definition files (pack.yaml)."""
from dataclasses import dataclass
from pathlib import Path

import yaml

SCHEMA = "protocols/pack/pack.schema.json"


@dataclass(frozen=True)
class Pack:
    root: Path
    name: str
    server: str
    project_file: str
    state_dir: str
    human_id: str
    human_prefix: str
    preamble: str
    def_keys: tuple
    plugin: dict
    mcp: dict
    hook: dict

    @classmethod
    def load(cls, root: Path) -> "Pack":
        from .validate import errors_for, load, schema_file, validator_for
        root = Path(root)
        m = yaml.safe_load((root / "pack.yaml").read_text())
        if errs := errors_for(validator_for(load(schema_file(SCHEMA, root)), root), m):
            raise ValueError(f"{root / 'pack.yaml'}: {errs[:3]}")
        return cls(root, m["name"], m["server"], m["project_file"], m["state_dir"], m["human"]["id"], m["human"]["tool_prefix"],
                   m["preamble"], tuple(m["def_keys"]), m["plugin"], m["mcp"], m["hook"])

    @property
    def tool_prefix(self) -> str:
        """Prefix of this pack's MCP tools as a session sees them."""
        return f"mcp__plugin_{self.name}_{self.server}__"
