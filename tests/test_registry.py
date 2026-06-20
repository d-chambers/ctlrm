from pathlib import Path

from ctlrm.models import ParticipantKind, Provider
from ctlrm.registry import Registry


def test_loads_participants_from_toml() -> None:
    data = """
[[projects]]
id = "ctlrm"
name = "ctlrm"
root = "/repo"
tmux_session = "ctlrm"

[[projects.participants]]
id = "codex-impl"
name = "Codex"
role = "implementer"
kind = "agent"
provider = "codex"
workdir = "/repo"

[[projects.participants]]
id = "derrick"
name = "Derrick"
role = "product-owner"
kind = "human"
workdir = "/repo"
"""

    registry = Registry.from_toml(data)

    assert len(registry.projects) == 1
    assert registry.projects[0].participants[0].provider is Provider.CODEX
    assert registry.projects[0].participants[1].kind is ParticipantKind.HUMAN


def test_missing_registry_loads_empty(tmp_path: Path) -> None:
    registry = Registry.load(tmp_path / "registry.toml")

    assert registry.projects == []


def test_save_atomic_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    registry = Registry(projects=[])

    registry.save_atomic(path)

    assert Registry.load(path) == registry
    assert not path.with_suffix(".toml.tmp").exists()
