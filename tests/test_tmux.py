from ctlrm.tmux import TmuxCommand, TmuxTargetRef


def test_formats_send_keys_command() -> None:
    target = TmuxTargetRef(session="ctlrm", window="agents", pane="%12")

    command = TmuxCommand.send_keys(target, "hello")

    assert command.program == "tmux"
    assert command.args == ["send-keys", "-t", "ctlrm:agents.%12", "hello", "Enter"]


def test_formats_capture_pane_command() -> None:
    target = TmuxTargetRef(session="ctlrm", window="agents", pane="%12")

    command = TmuxCommand.capture_pane(target)

    assert command.args == ["capture-pane", "-p", "-t", "ctlrm:agents.%12"]
