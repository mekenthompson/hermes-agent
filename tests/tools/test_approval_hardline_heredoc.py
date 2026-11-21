"""The shutdown/reboot hardline floor does not read heredoc data bodies as commands (HF-792).

A line that merely STARTS with ``reboot`` / ``halt`` / ``shutdown`` inside a body that a sole
``[sudo] cat|tee <<EOF`` only prints used to produce an unapprovable "Hardline block: system
shutdown/reboot". That is the only exempt shape. Real commands, bodies a shell or interpreter runs,
file writes (which something could run later) and anything after the heredoc still block.
"""

from __future__ import annotations

import logging

import pytest

from tools.approval import check_all_command_guards
from tools.approval_detection import detect_hardline_command
from tools.approval_floors import _hardline_block_result

BLOCKED = "system shutdown/reboot"


@pytest.mark.parametrize("command", [
    "cat <<EOF\nHF-761: resume\n\nreboot handling\nEOF",
    "cat <<'EOF'\nreboot the box at 5pm\nEOF",
    "sudo cat <<'EOF'\nshutdown now\nEOF",
    "tee <<EOF\nreboot\nEOF",
    "tee -a <<'EOF'\nshutdown at 5pm\nEOF",
    "/bin/cat -n <<EOF\nhalt the loop\nEOF",
    "cat <<-EOF\n\treboot\n\tEOF",
    "cat <<'A' <<'B'\nreboot\nA\npoweroff\nB\n",
])
def test_printed_heredoc_bodies_are_not_commands(command):
    assert detect_hardline_command(command) == (False, None)


# HF-792 review round 1: each of these blocks on base 24dbdde and passed the floor on 566e3833.
@pytest.mark.parametrize("command", [
    "python3 - <<'EOF'\nimport os\nos.system('reboot')\nEOF",          # interpreter bodies are code
    "python3 <<'EOF'\nimport os\nos.execvp('reboot', ['reboot'])\nEOF",
    "python3 - <<EOF\nx = 1\nhalt the loop\nEOF",
    "python3 <<'PY'\nx = 1\nshutdown = 3\nPY",
    "node <<EOF\nshutdown now\nEOF",
    "perl <<'EOF'\nsystem('reboot')\nEOF",
    "ruby <<'EOF'\nexec('reboot')\nEOF",
    "php <<'EOF'\nreboot\nEOF",
    "cat <<'EOF' > x\nreboot\nEOF\ntimeout 5 /bin/sh x",                # write-then-run
    "cat <<'EOF' > x\nreboot\nEOF\npython3 x",
    "cat <<'EOF' > x\nreboot\nEOF\nsystemd-run --wait x",
    "cat <<'EOF' > x\nreboot\nEOF\ncrontab x",
    "cat > ~/.bashrc <<'EOF'\nreboot\nEOF",                               # a file write of any kind
    "cat > notes.md <<EOF\nHF-761: resume\n\nreboot handling\nEOF",
    "cat <<'EOF' >> x\nreboot\nEOF",
    "cat <<'EOF' 2>x\nreboot\nEOF",
    "tee f <<EOF\nreboot\nEOF",
    "sudo tee /etc/motd <<EOF\nshutdown at 5pm\nEOF",
    "cat <<'EOF' | crontab -\nreboot\nEOF",
    "cat > a <<EOF\nreboot\nEOF\ncat > b <<'EOF'\npoweroff\nEOF\nls",
    "cat <<'EOF'\nreboot\nEOF\nls",                                       # anything after the heredoc
    "ls\ncat <<'EOF'\nreboot\nEOF",                                       # ...or before it
    "sudo -u root cat <<'EOF' > /etc/rc.local\nreboot\nEOF",
])
def test_interpreter_bodies_and_file_writes_stay_on_the_floor(command):
    verdict = detect_hardline_command(command)
    assert verdict[0] is True and verdict[1] in {BLOCKED, "systemctl poweroff/reboot"}, verdict


@pytest.mark.parametrize("command", [
    "reboot",
    "sudo reboot",
    "echo x; shutdown -h now",
    "bash -c 'reboot'",
    "ls && poweroff",
    "cat > f <<EOF\nhi\nEOF\nreboot",                    # a real command after the terminator
    "cat > f <<EOF\nhi\nEOF\n; shutdown -h now",
    "bash <<EOF\nreboot\nEOF",                           # bodies a shell runs ARE commands
    "sh <<'EOF'\nls\nshutdown -h now\nEOF",
    "zsh <<EOF\nhalt\nEOF",
    "sudo bash <<EOF\nreboot\nEOF",
    "ssh host <<EOF\nreboot\nEOF",
    "cat <<EOF | sh\nreboot\nEOF",
    "cat <<EOF $(reboot)\nx\nEOF",                       # a substitution in the opener runs
    "cat <<EOF\n$(reboot)\nEOF",                         # ...and one in an expanded body
    "cat <<EOF\n`reboot`\nEOF",
    "cat <<EOF\ntrue; \"reboot\"\nEOF",                  # an expanded body holding shell syntax is not text
    "tee f <<EOF\nls; reboot\nEOF",
    "cat > f <<EOF\nx && shutdown -h now\nEOF",
    "cat > j.sh <<'EOF'\nreboot\nEOF\nbash j.sh",        # the same command runs what it wrote
    "cat > j.sh <<EOF\nreboot\nEOF\nchmod +x j.sh && ./j.sh",
    "cat > j.sh <<EOF\nreboot\nEOF\n/tmp/j.sh",
    "cat > j.sh <<EOF\nreboot\nEOF\nsource j.sh",
    "cat <<EOF > j\nshutdown now\nEOF\nxargs sh < j",
    "python3 - <<EOF\nreboot\nEOF\nreboot",
    "cat <<EOF\nreboot\n",                                # unterminated: nothing is dropped
])
def test_real_power_commands_and_runnable_bodies_still_block(command):
    verdict = detect_hardline_command(command)
    assert verdict[0] is True and verdict[1] in {BLOCKED, "systemctl poweroff/reboot"}, verdict


def test_other_hardline_rules_still_read_data_bodies():
    """Only the shutdown/reboot family changed: a root wipe in a body is judged as before."""
    assert detect_hardline_command("cat > f <<EOF\nrm -rf /\nEOF")[0] is True


def test_a_blocked_command_is_logged_in_full_but_bounded(caplog):
    command = "reboot " + "x" * 6000
    with caplog.at_level(logging.WARNING, logger="tools.approval"):
        result = _hardline_block_result(BLOCKED, command)
    assert result["hardline"] is True
    (record,) = [r for r in caplog.records if "Hardline block" in r.getMessage()]
    message = record.getMessage()
    assert message.startswith(f"Hardline block ({BLOCKED}): reboot xxx") and "more characters" in message
    assert len(message) < 4200


def test_the_guard_blocks_a_real_command_and_lets_a_data_heredoc_through(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    assert check_all_command_guards("reboot", "local")["hardline"] is True
    allowed = check_all_command_guards("cat <<EOF\nreboot handling\nEOF", "local")
    assert not allowed.get("hardline")
    written = check_all_command_guards("cat > notes.md <<EOF\nreboot handling\nEOF", "local")
    assert written.get("hardline") is True
