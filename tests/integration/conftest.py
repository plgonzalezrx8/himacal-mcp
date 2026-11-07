import imaplib
import os
import socket
import time
import uuid

import caldav
import pytest

from himacal.config import load_settings


@pytest.fixture(scope="session")
def provider_settings():
    if os.environ.get("HIMACAL_INTEGRATION") != "1":
        pytest.skip("Run tests/integration/run.py for isolated provider acceptance")
    settings = load_settings()
    # Fail before any network call if someone points this suite at real account configuration.
    assert set(settings.accounts) == {"alpha", "beta"}
    for account_id, account in settings.accounts.items():
        assert account.email["imap"]["server"] == "imap://greenmail:3143"
        assert account.email["smtp"]["server"] == "smtp://smtp-fixture:3025"
        assert account.caldav.url == "http://radicale:5232/"
        assert account.caldav.username == account_id
        assert account.caldav.password_file.read_text().strip() == f"{account_id}-fixture-password"
    for hostname, port in (("greenmail", 3143), ("smtp-fixture", 3025), ("radicale", 5232)):
        for _ in range(30):
            try:
                with socket.create_connection((hostname, port), timeout=2):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise AssertionError(f"Disposable provider not ready: {hostname}:{port}")
    for account_id in settings.accounts:
        with imaplib.IMAP4("greenmail", 3143) as mail:
            mail.login(account_id, f"{account_id}-fixture-password")
            for name in ("Sent", "Trash", "Archive", "Copies"):
                mail.create(name)
    return settings


@pytest.fixture
def live_calendar(provider_settings):
    clients = []
    calendars = {}
    for account_id in ("alpha", "beta"):
        client = caldav.DAVClient(
            url="http://radicale:5232/",
            username=account_id,
            password=f"{account_id}-fixture-password",
        )
        clients.append(client)
        calendars[account_id] = client.principal().make_calendar(
            name=f"acceptance-{uuid.uuid4().hex}"
        )
    yield calendars
    for client in clients:
        client.close()
