import pytest

from orchestrator_kit.verify import _deploy_version


@pytest.mark.parametrize("v,ev,ok", [
    ("27e0882", {}, False), ("27E0882A" * 5, {}, False), ("a" * 40, {}, False),
    ("3f1c2d4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f", {}, True), ("v1.4.2", {}, True),
    ("v1.4.2", {"commit": "v1.4.2"}, False), ("  ", {}, False), ("a" * 41, {}, True),
])
def test_deploy_version(v, ev, ok):
    assert (_deploy_version("version", v, ev, None, set()) is None) == ok
