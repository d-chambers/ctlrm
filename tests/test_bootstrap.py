"""Public import and command-entrypoint smoke checks."""

import importlib
from typer.testing import CliRunner
from ctlrm.__main__ import app


class TestBootstrap:
    """The retained package surfaces load without old module paths."""

    def test_public_imports(self) -> None:
        """Catch moved modules that pass unrelated room tests."""
        for module in [
            "ctlrm",
            "ctlrm.__main__",
            "ctlrm.managed.projects",
            "ctlrm.managed.workflows",
            "ctlrm.communication.terminal",
            "ctlrm.web.app",
            "ctlrm.runtime.room",
        ]:
            importlib.import_module(module)

    def test_help(self) -> None:
        """The installed command surface advertises room operations."""
        result = CliRunner().invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.stdout and "inbox" in result.stdout
