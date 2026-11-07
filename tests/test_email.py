import base64
import json
import subprocess
import tomllib
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from himacal.config import AccountConfig, Settings
from himacal.email import AttachmentInput, EmailService, MessageRef
from himacal.errors import ProviderError


def command_args(argv):
    args = argv[1:]
    while args and args[0].startswith("--"):
        args = args[1:]
    return args


class FakeHimalaya:
    """Small release-shaped subprocess boundary, with no live mail service."""

    def __init__(self):
        self.calls = []
        self.validities = [73]
        self.failed = None
        self.timeout = False
        self.sent = []
        self.mailboxes = [
            {"id": "INBOX", "name": "INBOX", "role": "inbox"},
            {"id": "Deleted Items", "name": "Deleted Items", "role": "trash"},
        ]
        self.nested = b"From: nested@example.test\r\nSubject: Nested\r\n\r\nNested message\r\n"
        self.raw_message = (
            b"From: Alice <alice@example.test>\r\nTo: owner@example.test\r\n"
            b"Subject: Original\r\nMessage-ID: <a@example.test>\r\n"
            b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\nContent-Type: text/plain\r\n\r\nOriginal text\r\n"
            b"--outer\r\nContent-Type: application/x-example\r\n"
            b'Content-Disposition: attachment; filename="report.bin"\r\n'
            b"Content-Transfer-Encoding: binary\r\n\r\nabc\xff\r\n"
            b"--outer\r\nContent-Type: text/plain; charset=iso-8859-1\r\n"
            b'Content-Disposition: attachment; filename="note.txt"\r\n'
            b"Content-Transfer-Encoding: 8bit\r\n\r\ncaf\xe9\r\n"
            b"--outer\r\nContent-Type: message/rfc822\r\n"
            b'Content-Disposition: attachment; filename="nested.eml"\r\n\r\n'
            + self.nested
            + b"\r\n--outer--\r\n"
        )

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        args = command_args(argv)
        if self.failed and args[:2] == list(self.failed):
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 1, b"", b"password=secret provider details")
        if args[:2] == ["imap", "status"]:
            value = self.validities.pop(0) if len(self.validities) > 1 else self.validities[0]
            data = {"uid_validity": value}
        elif args[:2] == ["mailbox", "list"]:
            data = {"mailboxes": self.mailboxes}
        elif args[:2] in (["envelope", "list"], ["envelope", "search"]):
            data = {"envelopes": [{"id": "42", "subject": "hello"}], "queued": 0}
        elif args[:2] == ["message", "read"]:
            if "--raw" in args:
                return subprocess.CompletedProcess(argv, 0, self.raw_message, b"")
            data = {"headers": ["Original"], "body": "Original text"}
        elif args[:2] == ["message", "send"]:
            if "--backend=imap" in argv:
                return subprocess.CompletedProcess(
                    argv, 1, b'{"error":"No send-capable backend"}', b""
                )
            self.sent.append(kwargs["input"])
            data = {"message": "Message successfully sent"}
        else:
            data = {"message": "ok"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(data).encode(), b"")


@pytest.fixture
def service(monkeypatch):
    cli = FakeHimalaya()
    monkeypatch.setattr(subprocess, "run", cli)
    settings = Settings(
        accounts={
            "first": AccountConfig(email={"email": "owner@example.test", "imap": {"host": "one"}}),
            "second": AccountConfig(email={"email": "other@example.test", "imap": {"host": "two"}}),
        },
        mcp_password="private",
    )
    service = EmailService(settings)
    yield service, cli
    service.close()


def ref(uid_validity=73):
    return MessageRef(
        account_id="first", mailbox="INBOX", backend="imap", id="42", uid_validity=uid_validity
    )


@pytest.mark.parametrize(
    "backend,native_id,alias,allowed",
    [
        ("imap", "Inbox", "INBOX", True),
        ("imap", "Archive", "ARCHIVE", False),
        ("maildir", "Inbox", "INBOX", False),
    ],
)
def test_only_imap_reserved_inbox_has_case_insensitive_identity(
    monkeypatch, backend, native_id, alias, allowed
):
    cli = FakeHimalaya()
    cli.mailboxes = [{"id": native_id, "name": native_id, "role": native_id.lower()}]
    monkeypatch.setattr(subprocess, "run", cli)
    account = AccountConfig(
        email={
            "email": "owner@example.test",
            backend: {},
            "mailbox": {"alias": {native_id.lower(): alias}},
        }
    )
    email = EmailService(Settings(accounts={"first": account}, mcp_password="private"))
    try:
        if allowed:
            result = email.email_list_messages("first", native_id)
            assert result["messages"][0]["reference"]["mailbox"] == native_id
        else:
            with pytest.raises(ProviderError) as failure:
                email.email_list_messages("first", native_id)
            assert failure.value.code == "provider_error"
    finally:
        email.close()


def test_accounts_and_lists_create_scoped_references_without_read_side_effects(service):
    email, cli = service
    assert {a["id"] for a in email.email_list_accounts()["accounts"]} == {"first", "second"}
    messages = email.email_list_messages("first", "INBOX", page=2, page_size=5)
    reference = messages["messages"][0]["reference"]
    assert reference == {
        "account_id": "first",
        "mailbox": "INBOX",
        "backend": "imap",
        "id": "42",
        "uid_validity": 73,
    }
    assert email.email_read_message(MessageRef(**reference))["message"]["body"] == "Original text"
    for args, kwargs in cli.calls:
        assert args[1] == "--json" and args[2] == "--account=first"
        assert (
            tomllib.loads(Path(args[3].split("=", 1)[1]).read_text())["accounts"]["first"]["imap"][
                "host"
            ]
            == "one"
        )
        assert "--seen" not in args and kwargs.get("shell", False) is False


def test_stale_or_changing_uidvalidity_refuses_results_and_writes(service):
    email, cli = service
    cli.validities = [73, 74]
    with pytest.raises(ProviderError) as exc:
        email.email_list_messages("first", "INBOX")
    assert exc.value.code == "stale_reference"
    cli.validities = [74]
    with pytest.raises(ProviderError) as exc:
        email.email_move_message(ref(), "Deleted Items")
    assert exc.value.code == "stale_reference"
    assert not any(command_args(args)[:2] == ["message", "move"] for args, _ in cli.calls)


def test_search_keeps_option_like_values_in_the_selected_account(service):
    email, cli = service
    cli.mailboxes.append({"id": "--untrusted", "name": "--untrusted", "role": None})
    result = email.email_search_messages("second", "--untrusted", "--seen", page=2)
    assert result["messages"][0]["reference"]["account_id"] == "second"
    for argv, _ in cli.calls:
        assert "--account=second" in argv
    search = next(argv for argv, _ in cli.calls if command_args(argv)[:2] == ["envelope", "search"])
    assert "--mailbox=--untrusted" in search
    assert search[-2:] == ["--", "--seen"]
    status = next(argv for argv, _ in cli.calls if command_args(argv)[:2] == ["imap", "status"])
    assert status[-2:] == ["--", "--untrusted"]


def test_trash_is_a_move_and_deleted_flags_are_rejected(service):
    email, cli = service
    email.email_trash_message(ref())
    action = next(args for args, _ in cli.calls if command_args(args)[:2] == ["message", "move"])
    assert "--to=Deleted Items" in action and "--from=INBOX" in action
    with pytest.raises((ProviderError, ValueError)):
        email.email_add_flags(ref(), ["deleted"])
    assert not any("delete" == arg or "expunge" == arg for args, _ in cli.calls for arg in args)


def test_sending_and_forwarding_preserve_attachment_bytes_and_mime(service):
    email, cli = service
    attachment = AttachmentInput(
        filename="picture.bin", mime="image/png", base64=base64.b64encode(b"png").decode()
    )
    email.email_send_message(
        "first", ["recipient@example.test"], "Subject", "Hello", attachments=[attachment]
    )
    email.email_forward_message(ref(), ["recipient@example.test"], "See attached")
    sent = [BytesParser(policy=policy.default).parsebytes(raw) for raw in cli.sent]
    assert str(sent[0]["From"]) == "owner@example.test"
    first = next(iter(sent[0].iter_attachments()))
    assert (first.get_filename(), first.get_content_type(), first.get_payload(decode=True)) == (
        "picture.bin",
        "image/png",
        b"png",
    )
    forwarded = list(sent[1].iter_attachments())
    assert [
        (part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
        for part in forwarded[:2]
    ] == [
        ("report.bin", "application/x-example", b"abc\xff"),
        ("note.txt", "text/plain", b"caf\xe9"),
    ]
    assert forwarded[2].get_filename() == "nested.eml"
    assert forwarded[2].get_content_type() == "message/rfc822"
    assert forwarded[2].get_payload(0).as_bytes(policy=policy.SMTP) == cli.nested
    assert "Original" in str(sent[1]["Subject"])


def test_attachment_get_does_not_return_host_paths_and_removes_temp_files(service):
    email, cli = service
    result = email.email_get_attachment(ref(), "1")
    assert result["attachment"]["base64"] == base64.b64encode(b"abc\xff").decode()
    assert "path" not in result["attachment"]
    config_path = Path(
        next(
            arg.split("=", 1)[1]
            for args, _ in cli.calls
            for arg in args
            if arg.startswith("--config=")
        )
    )
    assert sorted(p.name for p in config_path.parent.iterdir()) == ["config.toml"]


@pytest.mark.parametrize(
    "attachment",
    [
        {"filename": "../secret", "mime": "text/plain", "base64": "YWJj"},
        {"filename": "file.txt", "mime": "text/plain", "base64": "@@@"},
        {"filename": "file.txt", "mime": "text/plain\r\nX-Bad: yes", "base64": "YWJj"},
        {
            "filename": "large.bin",
            "mime": "application/octet-stream",
            "base64": base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode(),
        },
    ],
)
def test_attachment_validation_prevents_send(service, attachment):
    email, cli = service
    with pytest.raises((ProviderError, ValueError)):
        email.email_send_message(
            "first",
            ["r@example.test"],
            "Subject",
            "Body",
            attachments=[AttachmentInput(**attachment)],
        )
    assert not cli.sent


def test_aggregate_attachment_limit_prevents_send(service):
    email, cli = service
    encoded = base64.b64encode(b"x" * (9 * 1024 * 1024)).decode()
    attachment = AttachmentInput(
        filename="part.bin", mime="application/octet-stream", base64=encoded
    )
    with pytest.raises(ProviderError) as exc:
        email.email_send_message(
            "first", ["r@example.test"], "Subject", "Body", attachments=[attachment] * 3
        )
    assert exc.value.code == "invalid_input"
    assert not cli.sent


def test_unsafe_nested_email_is_not_forwarded_after_partial_decoding(service):
    email, cli = service
    cli.raw_message = cli.raw_message.replace(cli.nested, cli.nested + b"\xff")
    with pytest.raises(ProviderError) as exc:
        email.email_forward_message(ref(), ["r@example.test"], "Body")
    assert exc.value.code == "unsupported"
    assert not cli.sent


def test_nested_mime_and_unsafe_incoming_filenames_keep_decoded_bytes(service):
    email, cli = service
    cli.raw_message = (
        b"From: a@example.test\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
        b"--outer\r\nContent-Type: multipart/related; boundary=inner\r\n\r\n"
        b"--inner\r\nContent-Type: text/html\r\n\r\nHello\r\n"
        b"--inner\r\nContent-Type: text/plain; charset=iso-8859-1\r\n"
        b'Content-Disposition: attachment; filename="../../note.txt"\r\n'
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\ncaf=E9\r\n"
        b"--inner--\r\n--outer--\r\n"
    )
    attachment = email.email_list_attachments(ref())["attachments"][0]
    assert "/" not in attachment["filename"] and "\\" not in attachment["filename"]
    assert (
        base64.b64decode(
            email.email_get_attachment(ref(), attachment["id"])["attachment"]["base64"]
        )
        == b"caf\xe9"
    )


def test_excessive_mime_part_count_fails_without_returning_partial_results(service):
    email, cli = service
    cli.raw_message = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
        + b"--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n" * 1001
        + b"--x--\r\n"
    )
    with pytest.raises(ProviderError) as exc:
        email.email_list_attachments(ref())
    assert exc.value.code == "unsupported"


@pytest.mark.parametrize("timeout", [False, True])
def test_ambiguous_write_failure_is_unknown_and_never_retried_or_exposed(service, timeout):
    email, cli = service
    cli.failed, cli.timeout = ("message", "send"), timeout
    with pytest.raises(ProviderError) as exc:
        email.email_send_message("first", ["r@example.test"], "Subject", "Body")
    assert exc.value.code == "outcome_unknown"
    assert "secret" not in str(exc.value)
    assert sum(args[-2:] == ["message", "send"] for args, _ in cli.calls) == 1
