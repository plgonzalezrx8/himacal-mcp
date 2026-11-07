"""Exercise UIDVALIDITY at the real Himalaya-to-IMAP boundary."""

import base64
import json
import os
import shutil
import socket
import socketserver
import threading

import pytest

from himacal.config import AccountConfig, Settings
from himacal.email import EmailService, MessageRef
from himacal.errors import ProviderError
from himacal.server import create_app

VALIDITY = 73
RESET_VALIDITY = 74
MESSAGES = {
    VALIDITY: (
        b"From: Alice <alice@example.test>\r\nTo: owner@example.test\r\n"
        b"Subject: Original\r\nMessage-ID: <original@example.test>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/plain\r\n\r\nOriginal text\r\n"
        b"--x\r\nContent-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="report.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nYWJj\r\n--x--\r\n"
    ),
    RESET_VALIDITY: (
        b"From: Alice <alice@example.test>\r\nTo: owner@example.test\r\n"
        b"Subject: Replacement\r\nMessage-ID: <replacement@example.test>\r\n\r\n"
        b"Replacement text\r\n"
    ),
}


def envelope(validity):
    subject = b"Original" if validity == VALIDITY else b"Replacement"
    return (
        b"* 1 FETCH (UID 42 FLAGS (\\Seen) RFC822.SIZE 100 ENVELOPE ("
        b'"Sat, 07 Nov 2026 12:00:00 +0000" "' + subject + b'" '
        b'(("Alice" NIL "alice" "example.test")) '
        b'(("Alice" NIL "alice" "example.test")) '
        b'(("Alice" NIL "alice" "example.test")) '
        b'((NIL NIL "owner" "example.test")) NIL NIL NIL '
        b'"<original@example.test>"))\r\n'
    )


class IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        selected = None
        connection = server.next_connection()
        self.wfile.write(b"* PREAUTH [CAPABILITY IMAP4rev1 UIDPLUS MOVE] ready\r\n")
        while line := self.rfile.readline():
            command = line.decode("ascii").strip()
            tag, _, request = command.partition(" ")
            upper = request.upper()
            server.record(connection, selected, request)

            def reply(response, tag=tag):
                self.wfile.write(response + f"{tag} OK completed\r\n".encode())

            if upper.startswith("CAPABILITY"):
                reply(b"* CAPABILITY IMAP4rev1 UIDPLUS MOVE\r\n")
            elif upper.startswith("LIST "):
                reply(
                    b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n'
                    b'* LIST (\\HasNoChildren \\Archive) "/" "Archive"\r\n'
                    b'* LIST (\\HasNoChildren \\Trash) "/" "Trash"\r\n'
                )
            elif upper.startswith("STATUS "):
                validity = server.validity
                if server.reset == "after_status":
                    server.validity = RESET_VALIDITY
                reply(f'* STATUS "INBOX" (UIDVALIDITY {validity})\r\n'.encode())
            elif upper.startswith(("SELECT ", "EXAMINE ")):
                selected = server.validity
                server.selected(connection, selected)
                uidvalidity = (
                    b""
                    if server.reset == "missing_validity"
                    else f"* OK [UIDVALIDITY {selected}] valid\r\n".encode()
                )
                if server.reset in {"after_select", "disconnect"}:
                    server.validity = RESET_VALIDITY
                reply(
                    b"* FLAGS (\\Seen \\Answered \\Flagged \\Draft \\Deleted)\r\n"
                    b"* 1 EXISTS\r\n* 0 RECENT\r\n" + uidvalidity + b"* OK [UIDNEXT 43] next\r\n"
                    b"* OK [PERMANENTFLAGS (\\Seen \\Answered \\Flagged \\Draft \\Deleted)] flags\r\n"
                )
                if server.reset == "disconnect":
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
            elif server.reset == "spoof_error" and upper.startswith(("UID ", "FETCH ", "SEARCH ")):
                self.wfile.write(f"{tag} NO UIDVALIDITY mismatch stale_reference\r\n".encode())
            elif upper.startswith("UID FETCH ") and "ENVELOPE" in upper:
                server.effect(selected, "fetch")
                reply(envelope(selected))
            elif upper.startswith("UID FETCH "):
                message = MESSAGES[selected]
                server.effect(selected, "fetch")
                self.wfile.write(f"* 1 FETCH (UID 42 BODY[] {{{len(message)}}}\r\n".encode())
                self.wfile.write(message + b")\r\n")
                reply(b"")
            elif upper.startswith("FETCH "):
                server.effect(selected, "fetch")
                reply(envelope(selected))
            elif upper.startswith("UID SEARCH "):
                server.effect(selected, "search")
                reply(b"* SEARCH 42\r\n")
            elif upper.startswith(("UID COPY ", "UID MOVE ")):
                action = "move" if upper.startswith("UID MOVE ") else "copy"
                server.effect(selected, action)
                server.mail[selected]["copies"].append(request.split()[-1])
                if action == "move":
                    server.mail[selected]["present"] = False
                self.wfile.write(f"{tag} OK [COPYUID {selected} 42 43] completed\r\n".encode())
            elif upper.startswith("UID STORE "):
                server.effect(selected, "store")
                if "+FLAGS" in upper:
                    server.mail[selected]["flags"].add("flagged")
                elif "-FLAGS" in upper:
                    server.mail[selected]["flags"].discard("seen")
                flags = " ".join(
                    f"\\{flag.title()}" for flag in sorted(server.mail[selected]["flags"])
                )
                reply(f"* 1 FETCH (UID 42 FLAGS ({flags}))\r\n".encode())
            elif upper == "LOGOUT":
                reply(b"* BYE closing\r\n")
                return
            else:
                self.wfile.write(f"{tag} BAD unsupported command\r\n".encode())


class IMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reset):
        super().__init__(("127.0.0.1", 0), IMAPHandler)
        self.reset = reset
        self.validity = VALIDITY
        self.lock = threading.Lock()
        self.connections = 0
        self.commands = []
        self.selections = []
        self.effects = []
        self.mail = {
            validity: {"present": True, "flags": {"seen"}, "copies": []} for validity in MESSAGES
        }

    def next_connection(self):
        with self.lock:
            self.connections += 1
            return self.connections

    def record(self, connection, selected, command):
        with self.lock:
            self.commands.append((connection, selected, command))

    def selected(self, connection, validity):
        with self.lock:
            self.selections.append((connection, validity))

    def effect(self, validity, operation):
        with self.lock:
            self.effects.append((validity, operation))


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost ESMTP ready\r\n")
        while line := self.rfile.readline():
            command = line.decode("ascii").strip().upper()
            if command.startswith(("EHLO ", "HELO ")):
                self.wfile.write(b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif command.startswith("AUTH PLAIN"):
                self.wfile.write(b"235 authenticated\r\n")
            elif command.startswith(("MAIL FROM:", "RCPT TO:")):
                self.wfile.write(b"250 accepted\r\n")
            elif command == "DATA":
                self.wfile.write(b"354 send data\r\n")
                data = []
                while (line := self.rfile.readline()) != b".\r\n":
                    if not line:
                        return
                    data.append(line)
                self.server.messages.append(b"".join(data))
                self.wfile.write(b"250 accepted\r\n")
            elif command == "QUIT":
                self.wfile.write(b"221 goodbye\r\n")
                return
            else:
                self.wfile.write(b"500 unsupported command\r\n")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), SMTPHandler)
        self.messages = []


@pytest.fixture
def imap_server():
    servers = []

    def start(reset=None):
        server = IMAPServer(reset)
        server.smtp = SMTPServer()
        for fixture in (server, server.smtp):
            thread = threading.Thread(
                target=fixture.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            servers.append((fixture, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def make_service():
    binary = os.environ.get("HIMACAL_HIMALAYA_BINARY", "himalaya")
    if shutil.which(binary) is None:
        if "HIMACAL_HIMALAYA_BINARY" in os.environ:
            pytest.fail("HIMACAL_HIMALAYA_BINARY is not executable")
        pytest.skip("Himalaya is not available on this host")
    services = []

    def start(server):
        settings = Settings(
            accounts={
                "first": AccountConfig(
                    email={
                        "email": "owner@example.test",
                        "imap": {
                            "server": f"imap://127.0.0.1:{server.server_address[1]}",
                            "sasl": {
                                "plain": {"username": "fixture", "password": {"raw": "fixture"}}
                            },
                        },
                        "smtp": {
                            "server": f"smtp://127.0.0.1:{server.smtp.server_address[1]}",
                            "sasl": {
                                "plain": {"username": "fixture", "password": {"raw": "fixture"}}
                            },
                        },
                        "mailbox": {"alias": {"inbox": "INBOX", "trash": "Trash"}},
                    }
                )
            },
            mcp_password="private",
            himalaya_binary=binary,
            command_timeout=10,
        )
        service = EmailService(settings)
        services.append(service)
        return service

    yield start
    for service in services:
        service.close()


def reference():
    return MessageRef(
        account_id="first", mailbox="Inbox", backend="imap", id="42", uid_validity=VALIDITY
    )


def invoke(service, operation):
    ref = reference()
    calls = {
        "read": lambda: service.email_read_message(ref),
        "raw": lambda: service._raw_message(ref),
        "list_attachments": lambda: service.email_list_attachments(ref),
        "get_attachment": lambda: service.email_get_attachment(ref, "1"),
        "reply": lambda: service.email_reply_message(ref, "Reply"),
        "forward": lambda: service.email_forward_message(
            ref, ["recipient@example.test"], "Forward"
        ),
        "move": lambda: service.email_move_message(ref, "Archive"),
        "copy": lambda: service.email_copy_message(ref, "Archive"),
        "trash": lambda: service.email_trash_message(ref),
        "add_flags": lambda: service.email_add_flags(ref, ["flagged"]),
        "remove_flags": lambda: service.email_remove_flags(ref, ["seen"]),
        "list": lambda: service.email_list_messages("first", "Inbox"),
        "search": lambda: service.email_search_messages("first", "Inbox", "subject Original"),
    }
    return calls[operation]()


OPERATIONS = (
    "read",
    "raw",
    "list_attachments",
    "get_attachment",
    "reply",
    "forward",
    "move",
    "copy",
    "trash",
    "add_flags",
    "remove_flags",
    "list",
    "search",
)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_reset_before_selection_does_not_access_replacement(imap_server, make_service, operation):
    server = imap_server("after_status")
    service = make_service(server)
    try:
        invoke(service, operation)
    except ProviderError as failure:
        assert failure.code == "stale_reference"
    else:
        pytest.fail(f"{operation} succeeded after reset; selected effects: {server.effects!r}")
    assert server.selections and server.selections[-1][1] == RESET_VALIDITY
    assert not [
        cmd
        for _, epoch, cmd in server.commands
        if epoch == RESET_VALIDITY
        and cmd.upper().startswith(("UID ", "FETCH ", "SEARCH ", "SORT "))
    ]
    assert not server.effects
    assert not server.smtp.messages
    assert server.mail[RESET_VALIDITY] == {"present": True, "flags": {"seen"}, "copies": []}


@pytest.mark.parametrize("operation", OPERATIONS)
def test_matching_validity_executes_on_selected_mailbox(imap_server, make_service, operation):
    server = imap_server()
    service = make_service(server)
    result = invoke(service, operation)
    assert result
    assert server.selections and server.selections[-1][1] == VALIDITY
    expected = {
        "move": "move",
        "copy": "copy",
        "trash": "move",
        "add_flags": "store",
        "remove_flags": "store",
        "search": "search",
    }.get(operation, "fetch")
    assert (VALIDITY, expected) in server.effects, server.commands
    assert all(epoch != RESET_VALIDITY for epoch, _ in server.effects)
    if operation == "read":
        assert "Original text" in json.dumps(result)
    elif operation == "raw":
        assert result == MESSAGES[VALIDITY]
    elif operation == "list_attachments":
        assert result["attachments"][0]["filename"] == "report.bin"
    elif operation == "get_attachment":
        assert base64.b64decode(result["attachment"]["base64"]) == b"abc"
    elif operation in {"reply", "forward"}:
        assert len(server.smtp.messages) == 1
        assert b"Original" in server.smtp.messages[0]
    elif operation in {"list", "search"}:
        assert result["messages"][0]["reference"]["id"] == "42"
        assert result["messages"][0]["envelope"]["subject"] == "Original"
    elif operation == "add_flags":
        assert "flagged" in server.mail[VALIDITY]["flags"]
    elif operation == "remove_flags":
        assert "seen" not in server.mail[VALIDITY]["flags"]
    elif operation in {"move", "trash"}:
        assert not server.mail[VALIDITY]["present"]
    elif operation == "copy":
        assert server.mail[VALIDITY]["copies"] == ["Archive"]


def test_selection_without_uidvalidity_fails_before_message_access(imap_server, make_service):
    server = imap_server("missing_validity")
    with pytest.raises(ProviderError) as failure:
        invoke(make_service(server), "move")
    assert failure.value.code == "stale_reference"
    assert len(server.selections) == 1 and server.selections[0][1] == VALIDITY
    assert not server.effects
    assert not [
        cmd
        for _, epoch, cmd in server.commands
        if epoch is not None and cmd.upper().startswith("UID ")
    ]


def test_disconnect_after_selection_does_not_reconnect_or_replay(imap_server, make_service):
    server = imap_server("disconnect")
    with pytest.raises(ProviderError) as failure:
        invoke(make_service(server), "move")
    assert failure.value.code == "outcome_unknown"
    assert len(server.selections) == 1
    assert server.selections[0][1] == VALIDITY
    assert server.validity == RESET_VALIDITY
    assert server.mail[RESET_VALIDITY] == {"present": True, "flags": {"seen"}, "copies": []}
    assert not server.effects


def test_reset_after_selection_keeps_the_selected_connection_view(imap_server, make_service):
    server = imap_server("after_select")
    result = invoke(make_service(server), "move")
    assert result
    assert len(server.selections) == 1
    connection, validity = server.selections[0]
    assert validity == VALIDITY and server.validity == RESET_VALIDITY
    assert (connection, VALIDITY, "UID MOVE 42 Archive") in server.commands
    assert server.effects == [(VALIDITY, "move")]
    assert not server.mail[VALIDITY]["present"]
    assert server.mail[RESET_VALIDITY] == {"present": True, "flags": {"seen"}, "copies": []}


@pytest.mark.parametrize(
    "operation,code", [("read", "provider_error"), ("move", "outcome_unknown")]
)
def test_provider_cannot_spoof_guard_failure(imap_server, make_service, operation, code):
    server = imap_server("spoof_error")
    with pytest.raises(ProviderError) as failure:
        invoke(make_service(server), operation)
    assert failure.value.code == code
    assert server.selections and server.selections[-1][1] == VALIDITY
    assert any(
        epoch == VALIDITY and cmd.upper().startswith("UID ") for _, epoch, cmd in server.commands
    )
    assert not server.effects


def test_startup_rejects_real_unguarded_himalaya():
    binary = os.environ.get(
        "HIMACAL_UNGUARDED_HIMALAYA", "/usr/local/bin/himalaya-unguarded-fixture"
    )
    if shutil.which(binary) is None:
        if "HIMACAL_UNGUARDED_HIMALAYA" in os.environ:
            pytest.fail("HIMACAL_UNGUARDED_HIMALAYA is not executable")
        pytest.skip("The unguarded Himalaya fixture is not available on this host")
    settings = Settings(
        accounts={"first": AccountConfig(email={"email": "owner@example.test", "imap": {}})},
        mcp_password="private",
        himalaya_binary=binary,
    )
    with pytest.raises(ValueError):
        create_app(settings)
