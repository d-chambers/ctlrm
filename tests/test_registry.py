"""test registry.py."""

from pathlib import Path
from ctlrm.models import ParticipantKind, Provider
from ctlrm.registry import Registry


class TestRegistry:
    """TestRegistry."""

    def test_loads_participants_from_toml(self) -> None:
        """test loads participants from toml."""
        data = '\n[[projects]]\nid = "ctlrm"\nname = "ctlrm"\nroot = "/repo"\ntmux_session = "ctlrm"\n\n[[projects.participants]]\nid = "codex-impl"\nname = "Codex"\nrole = "implementer"\nkind = "agent"\nprovider = "codex"\nworkdir = "/repo"\n\n[[projects.participants]]\nid = "derrick"\nname = "Derrick"\nrole = "product-owner"\nkind = "human"\nworkdir = "/repo"\n'
        registry = Registry.from_toml(data)
        assert len(registry.projects) == 1
        assert registry.projects[0].participants[0].provider is Provider.CODEX
        assert registry.projects[0].participants[1].kind is ParticipantKind.HUMAN

    def test_missing_registry_loads_empty(self, tmp_path: Path) -> None:
        """test missing registry loads empty."""
        registry = Registry.load(tmp_path / "registry.toml")
        assert registry.projects == []

    def test_save_atomic_round_trips(self, tmp_path: Path) -> None:
        """test save atomic round trips."""
        path = tmp_path / "registry.toml"
        registry = Registry(projects=[])
        registry.save_atomic(path)
        assert Registry.load(path) == registry
        assert not path.with_suffix(".toml.tmp").exists()


class TestRegistryUpdates:
    """Concurrent updates preserve every worktree and participant."""

    def test_concurrent_registrations(self, tmp_path: Path) -> None:
        """Fresh locked reads avoid lost participants from stale caller snapshots."""
        from concurrent.futures import ThreadPoolExecutor
        from ctlrm.models import Project, Participant

        path = tmp_path / "registry.toml"
        one = Project(id="one", name="One", root=tmp_path / "one")
        two = Project(id="two", name="Two", root=tmp_path / "two")
        Registry(projects=[two]).save_atomic(path)

        def register(index: int) -> None:
            """Register from independent stale project snapshots."""
            participant = Participant.human(f"p{index}", "Person", "review", one.root)
            Registry.add_participant(path, one, participant)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(register, range(20)))
        result = Registry.load(path)
        assert {project.id for project in result.projects} == {"one", "two"}
        assert len(result.project_for(one).participants) == 20
        assert not list(tmp_path.glob("tmp*"))

    def test_corrupt_file_is_preserved(self, tmp_path: Path) -> None:
        """A malformed existing registry must not become an empty replacement."""
        import pytest
        from ctlrm.models import Project, Participant

        path = tmp_path / "registry.toml"
        path.write_text("invalid = [")
        project = Project(id="p", name="P", root=tmp_path)
        with pytest.raises(ValueError):
            Registry.add_participant(
                path, project, Participant.human("person", "Person", "review", tmp_path)
            )
        assert path.read_text() == "invalid = ["

    def test_root_identity(self, tmp_path: Path) -> None:
        """Canonical paths identify one worktree even when labels differ."""
        from ctlrm.models import Project

        registry = Registry(projects=[Project(id="one", name="One", root=tmp_path)])
        assert (
            registry.project_for(Project(id="alias", name="Alias", root=tmp_path / "sub/.."))
            is registry.projects[0]
        )


class TestRegistryInitialParticipants:
    """Initial registration preserves participants supplied with a worktree."""

    def test_existing_human_is_preserved(self, tmp_path: Path) -> None:
        """Adding an agent must retain an in-memory human on the first save."""
        from ctlrm.models import Project, Participant

        human = Participant.human("owner", "Owner", "approve", tmp_path)
        project = Project(id="project", name="Project", root=tmp_path, participants=[human])
        other = Participant.human("reviewer", "Reviewer", "review", tmp_path)
        path = tmp_path / "registry.toml"
        Registry.add_participant(path, project, other)
        assert [p.id for p in Registry.load(path).projects[0].participants] == ["owner", "reviewer"]


class TestRegistryCallerAdditions:
    """Persist caller additions without overwriting concurrent registry state."""

    def test_in_memory_human_survives_agent_save(self, tmp_path: Path) -> None:
        """A human added through Workspace remains after an agent is persisted."""
        from ctlrm.models import Project, Participant
        from ctlrm.workspace import Workspace

        path = tmp_path / "registry.toml"
        project = Project(id="p", name="P", root=tmp_path)
        Registry(projects=[project]).save_atomic(path)
        workspace = Workspace.from_projects([project])
        human = Participant.human("owner", "Owner", "approve", tmp_path)
        workspace.add_participant(human)
        agent = Participant.human("reviewer", "Reviewer", "review", tmp_path)
        Registry.add_participant(path, project, agent)
        assert {p.id for p in Registry.load(path).projects[0].participants} == {"owner", "reviewer"}


class TestRegistryIdentityOrder:
    """Worktree identity takes precedence over display IDs regardless of order."""

    def test_existing_root_wins_over_alias(self, tmp_path: Path) -> None:
        """An alias matching another entry cannot hide the canonical root."""
        from ctlrm.models import Project

        one = Project(id="one", name="One", root=tmp_path / "a")
        two = Project(id="two", name="Two", root=tmp_path / "b")
        alias = Project(id="one", name="Alias", root=two.root)
        for entries in ([one, two], [two, one]):
            assert Registry(projects=entries).project_for(alias) is two
