"""test models.py."""

from pathlib import Path
from ctlrm.models import Participant, ParticipantKind, ParticipantState, Provider, TmuxTarget


class TestModels:
    """TestModels."""

    def test_human_participant_has_no_provider_or_tmux(self) -> None:
        """test human participant has no provider or tmux."""
        participant = Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))
        assert participant.kind is ParticipantKind.HUMAN
        assert participant.provider is None
        assert participant.tmux_target is None
        assert participant.state is ParticipantState.IDLE

    def test_cli_agent_records_provider_and_tmux_target(self) -> None:
        """test cli agent records provider and tmux target."""
        participant = Participant.agent(
            "codex-impl",
            "Codex",
            "implementer",
            Provider.CODEX,
            Path("/repo"),
            TmuxTarget(session="ctlrm", window="agents", pane="%12"),
        )
        assert participant.kind is ParticipantKind.AGENT
        assert participant.provider is Provider.CODEX
        assert participant.tmux_target is not None
        assert participant.tmux_target.selector == "%12"
