"""Run acceptance with isolated local providers; never use the operator's accounts."""

import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def run() -> int:
    root = Path(__file__).resolve().parents[2]
    tag = uuid.uuid4().hex[:10]
    project = f"himacal-test-{tag}"
    fixtures = root / ".runtime" / project
    fixtures.mkdir(parents=True)
    password = secrets.token_urlsafe(32)
    (fixtures / "mcp_password").write_text(password, encoding="utf-8")
    for account in ("alpha", "beta"):
        (fixtures / f"{account}_password").write_text(
            f"{account}-fixture-password", encoding="utf-8"
        )
    config = []
    for account, secret_file in (("alpha", "provider_password"), ("beta", "beta_password")):
        config.append(f'''
[accounts.{account}.email]
email = "{account}@localhost"
imap.server = "imap://greenmail:3143"
# GreenMail requires the SASL continuation exchange despite its advertised capability.
imap.sasl-ir = false
imap.sasl.login.username = "{account}"
imap.sasl.login.password.command = ["cat", "/run/secrets/{secret_file}"]
smtp.server = "smtp://smtp-fixture:3025"
smtp.sasl.plain.username = "{account}"
smtp.sasl.plain.password.command = ["cat", "/run/secrets/{secret_file}"]
mailbox.alias.inbox = "INBOX"
mailbox.alias.sent = "Sent"
mailbox.alias.trash = "Trash"
message.send.save-copy = "Sent"

[accounts.{account}.caldav]
url = "http://radicale:5232/"
username = "{account}"
password_file = "/run/secrets/{secret_file}"
timezone = "America/New_York"
''')
    (fixtures / "himacal.toml").write_text("\n".join(config), encoding="utf-8")
    for path in fixtures.iterdir():
        path.chmod(0o644)
    fixtures.chmod(0o700)
    env = os.environ | {
        "HIMACAL_TEST_DIR": fixtures.as_posix(),
        "HIMACAL_TEST_TAG": tag,
        "HIMACAL_HOST_PORT": "0",
    }
    compose = [
        "docker",
        "compose",
        "--progress",
        "plain",
        "--project-name",
        project,
        "-f",
        str(root / "docker-compose.yml"),
        "-f",
        str(root / "tests/integration/docker-compose.yml"),
    ]

    def command(*args, capture=False):
        return subprocess.run(
            compose + list(args), cwd=root, env=env, check=True, text=True, capture_output=capture
        )

    try:
        command("build", "himacal", "tests", "radicale")
        command("up", "-d", "--wait", "--wait-timeout", "120", "himacal", "greenmail", "radicale")
        command("run", "--rm", "tests", "pytest", "-q", "--tb=short", *sys.argv[1:])
        port = command("port", "himacal", "8000", capture=True).stdout.strip().split(":")[-1]
        url = f"http://127.0.0.1:{port}/healthz"

        def health(token):
            try:
                request = Request(
                    url, headers={"Authorization": f"Bearer {token}", "Host": "mcp.example.test"}
                )
                with urlopen(request, timeout=5) as response:
                    return response.status
            except HTTPError as error:
                return error.code

        assert health(password) == 200
        command("restart", "himacal")
        # Docker may reassign a host port published as 0 when the container restarts.
        port = command("port", "himacal", "8000", capture=True).stdout.strip().split(":")[-1]
        url = f"http://127.0.0.1:{port}/healthz"
        for _ in range(30):
            try:
                if health(password) == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise AssertionError("Service did not recover after restart")
        replacement = secrets.token_urlsafe(32)
        (fixtures / "mcp_password").write_text(replacement, encoding="utf-8")
        command(
            "up", "-d", "--force-recreate", "--no-deps", "--wait", "--wait-timeout", "90", "himacal"
        )
        port = command("port", "himacal", "8000", capture=True).stdout.strip().split(":")[-1]
        url = f"http://127.0.0.1:{port}/healthz"
        assert health(password) == 401
        assert health(replacement) == 200
        label = "Selected checks" if sys.argv[1:] else "Acceptance"
        print(f"{label} passed: tests, root Compose, restart and password rotation.")
        return 0
    except (subprocess.CalledProcessError, AssertionError) as error:
        print(f"Acceptance failed: {error}", file=sys.stderr)
        subprocess.run(
            compose + ["logs", "--no-color", "--tail", "100"], cwd=root, env=env, check=False
        )
        return 1
    finally:
        # This unique Compose project was created by this invocation; no shared resources.
        subprocess.run(
            compose + ["down", "--volumes", "--remove-orphans"], cwd=root, env=env, check=False
        )
        for name in ("mcp_password", "alpha_password", "beta_password"):
            (fixtures / name).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(run())
