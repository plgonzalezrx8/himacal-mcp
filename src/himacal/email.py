"""Scoped, non-interactive access to the pinned Himalaya command line."""

import base64 as b64
import binascii
import copy
import json
import os
import quopri
import re
import subprocess
import tempfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from itertools import pairwise
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict

from himacal.config import Settings
from himacal.errors import ProviderError
from himacal.tools import register_tool

MAX_ATTACHMENT = 10 * 1024 * 1024
MAX_ATTACHMENTS = 25 * 1024 * 1024
BACKENDS = ("maildir", "m2dir", "mbox", "pimdir", "jmap", "gmail", "msgraph", "imap")
SAFE_FLAGS = {"seen", "answered", "flagged", "draft"}
MIME = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class MessageRef(BaseModel):
    """A message ID is meaningful only in its account, backend and mailbox."""

    model_config = ConfigDict(extra="forbid")
    account_id: str
    mailbox: str
    backend: str
    id: str
    uid_validity: int | None = None


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    mime: str
    base64: str


def _invalid(message: str) -> ProviderError:
    return ProviderError("invalid_input", message)


def _text(value: str, what: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise _invalid(f"Invalid {what}.")
    return value


def _header(value: str, what: str, *, empty: bool = False) -> str:
    _text(value, what, empty=empty)
    if "\r" in value or "\n" in value:
        raise _invalid(f"Invalid {what}.")
    return value


def _filename(name: str, *, incoming: bool = False) -> str:
    if incoming:
        name = (name or "attachment").replace("/", "_").replace("\\", "_")
        name = "".join(char if ord(char) >= 32 and char not in '<>:"|?*' else "_" for char in name)
        return name.strip().lstrip(".")[:200] or "attachment"
    _header(name, "attachment filename")
    if (
        name in {".", ".."}
        or name.startswith(".")
        or len(name) > 200
        or any(c in name for c in '/\\<>:"|?*')
        or any(ord(c) < 32 for c in name)
    ):
        raise _invalid("Invalid attachment filename.")
    return name


def _mime(value: str) -> str:
    if not isinstance(value, str) or not MIME.fullmatch(value):
        raise _invalid("Invalid attachment MIME type.")
    return value.lower()


def _decode_attachments(attachments: list[AttachmentInput] | None) -> list[tuple[str, str, bytes]]:
    decoded = []
    total = 0
    for item in attachments or []:
        name, mime = _filename(item.filename), _mime(item.mime)
        if len(item.base64) > ((MAX_ATTACHMENT + 2) // 3) * 4:
            raise _invalid("An attachment exceeds the 10 MiB limit.")
        try:
            content = b64.b64decode(item.base64, validate=True)
        except (ValueError, binascii.Error):
            raise _invalid("Attachment content must be valid base64.") from None
        total += len(content)
        if len(content) > MAX_ATTACHMENT or total > MAX_ATTACHMENTS:
            raise _invalid("Attachments exceed the allowed size.")
        decoded.append((name, mime, content))
    return decoded


def _addresses(values: list[str] | None, *, required: bool = False) -> list[str]:
    values = values or []
    for value in values:
        _header(value, "email address")
    try:
        parsed = getaddresses(values, strict=True)
    except ValueError:
        raise _invalid("Invalid email address.") from None
    if (required and not parsed) or any(not addr or "@" not in addr for _, addr in parsed):
        raise _invalid("Invalid email address.")
    return [formataddr((name, address)) for name, address in parsed]


def _mime_parts(raw: bytes) -> list[dict]:
    """Decode attachment bodies from MIME bytes without changing their charsets."""
    if len(raw) > 64 * 1024 * 1024:
        raise ProviderError("unsupported", "The message is too large to process attachments.")
    attachments = []
    visited = 0

    def visit(data: bytes, root: bool = False, depth: int = 0):
        nonlocal visited
        visited += 1
        if depth > 32 or visited > 1000:
            raise ProviderError("unsupported", "The message has too many nested MIME parts.")
        separator = re.search(rb"\r\n\r\n|\n\n|\r\r", data)
        if separator is None:
            raise ProviderError("provider_error", "The message contains invalid MIME data.")
        part = BytesHeaderParser(policy=policy.default).parsebytes(data[: separator.end()])
        body = data[separator.end() :]
        mime = part.get_content_type().lower()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        is_attachment = (
            disposition in {"attachment", "inline"}
            or filename is not None
            or (
                not root
                and not mime.startswith("multipart/")
                and mime not in {"text/plain", "text/html"}
            )
        )
        if is_attachment:
            encoding = (part.get("Content-Transfer-Encoding") or "").strip().lower()
            try:
                if encoding == "base64":
                    content = b64.b64decode(re.sub(rb"\s+", b"", body), validate=True)
                elif encoding == "quoted-printable":
                    content = quopri.decodestring(body)
                elif encoding in {"", "7bit", "8bit", "binary"}:
                    content = body
                else:
                    raise ValueError
            except (ValueError, binascii.Error):
                raise ProviderError(
                    "provider_error", "The attachment uses invalid MIME encoding."
                ) from None
            attachments.append(
                {
                    "id": str(len(attachments) + 1),
                    "filename": _filename(filename, incoming=True),
                    "mime": mime,
                    "size": len(content),
                    "inline": disposition == "inline",
                    "content": content,
                }
            )
        elif mime.startswith("multipart/"):
            boundary = part.get_boundary()
            if not boundary:
                raise ProviderError("provider_error", "The message contains invalid MIME data.")
            try:
                pattern = re.compile(
                    rb"(?m)^--" + re.escape(boundary.encode("ascii")) + rb"(--)?[ \t]*(?:\r?\n|$)"
                )
            except UnicodeEncodeError:
                raise ProviderError(
                    "provider_error", "The message contains invalid MIME data."
                ) from None
            markers = []
            for marker in pattern.finditer(body):
                markers.append(marker)
                if len(markers) > 1001:
                    raise ProviderError("unsupported", "The message has too many MIME parts.")
            if not markers or not markers[-1].group(1):
                raise ProviderError("provider_error", "The message contains invalid MIME data.")
            for start, end in pairwise(markers):
                if start.group(1):
                    break
                child = body[start.end() : end.start()]
                if child.endswith(b"\r\n"):
                    child = child[:-2]
                elif child.endswith(b"\n"):
                    child = child[:-1]
                visit(child, depth=depth + 1)

    visit(raw, root=True)
    return attachments


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._accounts = {
            name: copy.deepcopy(account.email)
            for name, account in settings.accounts.items()
            if account.email is not None
        }
        self._temp = tempfile.TemporaryDirectory(prefix="himacal-")
        os.chmod(self._temp.name, 0o700)
        self._config = Path(self._temp.name) / "config.toml"
        with self._config.open("wb") as stream:
            os.chmod(self._config, 0o600)
            tomli_w.dump({"accounts": self._accounts}, stream)

    def close(self):
        self._temp.cleanup()

    def _account(self, account_id: str) -> tuple[dict, str]:
        self.settings.require_account(account_id, "email")
        config = self._accounts.get(account_id)
        if config is None:
            raise ProviderError("account_not_found", "Unknown email account.")
        backend = next((name for name in BACKENDS if name in config), None)
        if backend is None:
            raise ProviderError("provider_error", "This email account has no storage backend.")
        return config, backend

    def _run(
        self,
        account_id: str,
        args: list[str],
        *,
        mutation: bool = False,
        input_data: bytes | None = None,
        raw_output: bool = False,
        expected_uid_validity: int | None = None,
    ) -> dict | bytes:
        _, backend = self._account(account_id)
        argv = [
            self.settings.himalaya_binary,
            *([] if raw_output else ["--json"]),
            f"--account={account_id}",
            f"--config={self._config}",
            *([] if args[:2] == ["message", "send"] else [f"--backend={backend}"]),
            *(
                []
                if expected_uid_validity is None
                else [f"--expected-uid-validity={expected_uid_validity}"]
            ),
            *args,
        ]
        try:
            result = subprocess.run(
                argv,
                input=input_data,
                stdin=None if input_data is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=self.settings.command_timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            if mutation:
                raise ProviderError(
                    "outcome_unknown", "The operation may have completed; verify before retrying."
                ) from None
            raise ProviderError(
                "provider_error", "The email provider did not respond in time."
            ) from None
        except OSError:
            raise ProviderError(
                "provider_error", "The email provider could not be started."
            ) from None
        if result.returncode:
            if expected_uid_validity is not None and result.returncode == 3:
                raise ProviderError(
                    "stale_reference", "The message reference is stale; list it again."
                )
            if mutation:
                raise ProviderError(
                    "outcome_unknown", "The operation may have completed; verify before retrying."
                )
            raise ProviderError(
                "provider_error", "The email provider could not complete the request."
            )
        if raw_output:
            return result.stdout
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise TypeError
            return data
        except (ValueError, TypeError, UnicodeDecodeError):
            if mutation:
                raise ProviderError(
                    "outcome_unknown", "The operation may have completed; verify before retrying."
                ) from None
            raise ProviderError(
                "provider_error", "The email provider returned an invalid response."
            ) from None

    def email_list_accounts(self) -> dict:
        """List configured email account identifiers."""
        return {"accounts": [{"id": name} for name in self._accounts]}

    def email_list_mailboxes(self, account_id: str) -> dict:
        """List the selected account's mailboxes and their provider IDs."""
        data = self._run(account_id, ["mailbox", "list"])
        if not isinstance(data.get("mailboxes"), list):
            raise ProviderError(
                "provider_error", "The email provider returned an invalid mailbox list."
            )
        return {"mailboxes": data["mailboxes"]}

    def _mailbox(self, account_id: str, value: str) -> str:
        _text(value, "mailbox")
        config, backend = self._account(account_id)
        mailboxes = self.email_list_mailboxes(account_id)["mailboxes"]
        aliases = config.get("mailbox", {}).get(
            "alias", config.get("mailbox", {}).get("aliases", {})
        )
        aliases = (
            {str(k).lower(): v for k, v in aliases.items()} if isinstance(aliases, dict) else {}
        )
        alias = aliases.get(value.lower())
        candidates = [m for m in mailboxes if isinstance(m, dict) and m.get("id") == value]
        if not candidates and alias:
            candidates = [m for m in mailboxes if isinstance(m, dict) and m.get("id") == alias]
        if not candidates:
            candidates = [
                m
                for m in mailboxes
                if isinstance(m, dict)
                and (m.get("name") == value or str(m.get("role", "")).lower() == value.lower())
            ]
        if len(candidates) != 1 or not isinstance(candidates[0].get("id"), str):
            raise ProviderError("mailbox_not_found", "The mailbox was not found or is ambiguous.")
        mailbox = candidates[0]["id"]
        # Himalaya resolves aliases even when a caller passes a native ID.
        mapped = aliases.get(mailbox.lower(), mailbox)
        same_inbox = backend == "imap" and mailbox.lower() == str(mapped).lower() == "inbox"
        if mapped != mailbox and not same_inbox:
            raise ProviderError("provider_error", "A mailbox alias conflicts with its provider ID.")
        return mailbox

    def _validity(self, account_id: str, backend: str, mailbox: str) -> int | None:
        if backend != "imap":
            return None
        result = self._run(account_id, ["imap", "status", "--", mailbox])
        value = result.get("uid_validity")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 0xFFFFFFFF:
            raise ProviderError("stale_reference", "The mailbox has no usable UIDVALIDITY.")
        return value

    def _check(self, reference: MessageRef) -> str:
        _text(reference.id, "message ID")
        _, backend = self._account(reference.account_id)
        if backend != reference.backend:
            raise ProviderError("stale_reference", "The message backend has changed.")
        mailbox = self._mailbox(reference.account_id, reference.mailbox)
        if mailbox != reference.mailbox:
            raise ProviderError("stale_reference", "The message mailbox has changed.")
        validity = self._validity(reference.account_id, backend, mailbox)
        if validity != reference.uid_validity:
            raise ProviderError("stale_reference", "The message reference is stale; list it again.")
        return mailbox

    def _read(self, reference: MessageRef, command: list[str]) -> dict:
        mailbox = self._check(reference)
        result = self._run(
            reference.account_id,
            [*command, f"--mailbox={mailbox}", "--", reference.id],
            expected_uid_validity=reference.uid_validity,
        )
        if (
            self._validity(reference.account_id, reference.backend, mailbox)
            != reference.uid_validity
        ):
            raise ProviderError("stale_reference", "The message reference is stale; list it again.")
        return result

    def _list(
        self, account_id: str, mailbox: str, page: int, page_size: int, query: str | None = None
    ) -> dict:
        if (
            isinstance(page, bool)
            or not 1 <= page <= 100000
            or isinstance(page_size, bool)
            or not 1 <= page_size <= 200
        ):
            raise _invalid("Page and page size are out of range.")
        _, backend = self._account(account_id)
        mailbox = self._mailbox(account_id, mailbox)
        validity = self._validity(account_id, backend, mailbox)
        args = [
            "envelope",
            "search" if query is not None else "list",
            f"--mailbox={mailbox}",
            f"--page={page}",
            f"--page-size={page_size}",
        ]
        if query is not None:
            args += ["--", _text(query, "search query")]
        data = self._run(account_id, args, expected_uid_validity=validity)
        if self._validity(account_id, backend, mailbox) != validity:
            raise ProviderError(
                "stale_reference", "The mailbox changed while listing; list it again."
            )
        envelopes = data.get("envelopes")
        if not isinstance(envelopes, list):
            raise ProviderError(
                "provider_error", "The email provider returned an invalid message list."
            )
        messages = []
        for envelope in envelopes:
            if (
                not isinstance(envelope, dict)
                or not isinstance(envelope.get("id"), str)
                or not envelope["id"]
            ):
                raise ProviderError(
                    "provider_error", "The email provider returned an invalid message ID."
                )
            reference = MessageRef(
                account_id=account_id,
                mailbox=mailbox,
                backend=backend,
                id=envelope["id"],
                uid_validity=validity,
            )
            messages.append({"reference": reference.model_dump(), "envelope": envelope})
        return {"messages": messages, "page": page, "page_size": page_size}

    def email_list_messages(
        self, account_id: str, mailbox: str, page: int = 1, page_size: int = 25
    ) -> dict:
        """List a page of messages without marking them read."""
        return self._list(account_id, mailbox, page, page_size)

    def email_search_messages(
        self, account_id: str, mailbox: str, query: str, page: int = 1, page_size: int = 25
    ) -> dict:
        """Search a mailbox with Himalaya's search query syntax."""
        return self._list(account_id, mailbox, page, page_size, query)

    def email_read_message(self, reference: MessageRef) -> dict:
        """Read a scoped message without changing its seen flag."""
        return {
            "reference": reference.model_dump(),
            "message": self._read(reference, ["message", "read"]),
        }

    def _raw_message(self, reference: MessageRef) -> bytes:
        mailbox = self._check(reference)
        raw = self._run(
            reference.account_id,
            ["message", "read", "--raw", f"--mailbox={mailbox}", "--", reference.id],
            raw_output=True,
            expected_uid_validity=reference.uid_validity,
        )
        if (
            self._validity(reference.account_id, reference.backend, mailbox)
            != reference.uid_validity
        ):
            raise ProviderError("stale_reference", "The message reference is stale; list it again.")
        return raw

    def _compose(
        self,
        account_id: str,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None,
        bcc: list[str] | None,
        attachments: list[tuple[str, str, bytes]],
        source=None,
        reply: bool = False,
    ) -> bytes:
        config, _ = self._account(account_id)
        sender = config.get("email") or config.get("from")
        if not isinstance(sender, str):
            raise ProviderError("provider_error", "The email account has no sender address.")
        senders = _addresses([sender], required=True)
        if len(senders) != 1:
            raise ProviderError(
                "provider_error", "The email account has an invalid sender address."
            )
        name, address = parseaddr(senders[0])
        display = config.get("display-name") or config.get("from-name") or name
        if display:
            _header(display, "sender name")
        recipients = _addresses(to, required=True)
        copies, blind_copies = _addresses(cc), _addresses(bcc)
        _header(subject, "subject", empty=True)
        _text(body, "body", empty=True)
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = formataddr((display or "", address))
        message["To"] = ", ".join(recipients)
        if copies:
            message["Cc"] = ", ".join(copies)
        if blind_copies:
            message["Bcc"] = ", ".join(blind_copies)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=address.rsplit("@", 1)[1])
        if source is not None:
            original_id = source.get("Message-ID")
            references = source.get("References") or source.get("In-Reply-To")
            if original_id:
                _header(str(original_id), "source message ID")
                if reply:
                    message["In-Reply-To"] = str(original_id)
                chain = f"{references or ''} {original_id}".strip()
                _header(chain, "source references")
                message["References"] = chain
            content = (
                source.get_body(preferencelist=("plain", "html"))
                if source.is_multipart()
                else source
            )
            try:
                original = content.get_content() if content else ""
            except (LookupError, UnicodeError, KeyError):
                original = ""
            if isinstance(original, str) and original.strip():
                quote = "\n".join("> " + line for line in original.strip().splitlines())
                body = f"{body}\n\n{quote}" if body else quote
        message.set_content(body)
        for filename, mime, data in attachments:
            maintype, subtype = mime.split("/", 1)
            if mime == "message/rfc822":
                # shortcut: reject noncanonical attached emails until the CLI preserves raw bytes.
                if (
                    not data.isascii()
                    or b"\x00" in data
                    or b"\n" in data.replace(b"\r\n", b"")
                    or b"\r" in data.replace(b"\r\n", b"")
                ):
                    raise ProviderError(
                        "unsupported",
                        "This attached email cannot be forwarded without changing its bytes.",
                    )
                if not message.is_multipart():
                    message.make_mixed()
                part = EmailMessage(policy=policy.SMTP)
                part["Content-Type"] = mime
                part["Content-Transfer-Encoding"] = "7bit"
                part.add_header("Content-Disposition", "attachment", filename=filename)
                part.set_payload(data.decode("ascii"))
                message.attach(part)
            else:
                message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        return message.as_bytes()

    def _send(self, account_id: str, raw: bytes) -> dict:
        result = self._run(account_id, ["message", "send"], mutation=True, input_data=raw)
        return {"accepted": True, "queued": result.get("queueId") is not None}

    def email_send_message(
        self,
        account_id: str,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[AttachmentInput] | None = None,
    ) -> dict:
        """Send a new email. Sending cannot reliably be undone."""
        parts = _decode_attachments(attachments)
        return self._send(account_id, self._compose(account_id, to, subject, body, cc, bcc, parts))

    def email_reply_message(
        self,
        reference: MessageRef,
        body: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[AttachmentInput] | None = None,
    ) -> dict:
        """Reply to a message. Sending cannot reliably be undone."""
        parts = _decode_attachments(attachments)
        source = BytesParser(policy=policy.default).parsebytes(self._raw_message(reference))
        recipients = to or [str(source.get("Reply-To") or source.get("From") or "")]
        original_subject = str(source.get("Subject") or "")
        subject = (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )
        raw = self._compose(
            reference.account_id, recipients, subject, body, cc, bcc, parts, source, True
        )
        self._check(reference)
        return self._send(reference.account_id, raw)

    def email_forward_message(
        self,
        reference: MessageRef,
        to: list[str],
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[AttachmentInput] | None = None,
    ) -> dict:
        """Forward a message with its attachments. Sending cannot reliably be undone."""
        parts = _decode_attachments(attachments)
        original = self._raw_message(reference)
        source = BytesParser(policy=policy.default).parsebytes(original)
        downloaded = _mime_parts(original)
        total = sum(len(data) for _, _, data in parts)
        with tempfile.TemporaryDirectory(prefix="attachment-", dir=self._temp.name) as directory:
            os.chmod(directory, 0o700)
            for item in downloaded:
                content = item["content"]
                total += len(content)
                if len(content) > MAX_ATTACHMENT or total > MAX_ATTACHMENTS:
                    raise _invalid("Attachments exceed the allowed size.")
                path = Path(directory) / item["id"]
                path.write_bytes(content)
                parts.append((item["filename"], _mime(item["mime"]), path.read_bytes()))
        original_subject = str(source.get("Subject") or "")
        subject = (
            original_subject
            if original_subject.lower().startswith("fwd:")
            else f"Fwd: {original_subject}"
        )
        raw = self._compose(reference.account_id, to, subject, body, cc, bcc, parts, source)
        self._check(reference)
        return self._send(reference.account_id, raw)

    def _transfer(
        self, reference: MessageRef, destination: str, operation: Literal["move", "copy"]
    ) -> dict:
        target = self._mailbox(reference.account_id, destination)
        source = self._check(reference)
        result = self._run(
            reference.account_id,
            ["message", operation, f"--from={source}", f"--to={target}", "--", reference.id],
            mutation=True,
            expected_uid_validity=reference.uid_validity,
        )
        return {"result": result.get("message", "Operation completed.")}

    def email_move_message(self, reference: MessageRef, destination: str) -> dict:
        """Move a message, including restoring one from Trash."""
        return self._transfer(reference, destination, "move")

    def email_copy_message(self, reference: MessageRef, destination: str) -> dict:
        """Copy a message to another mailbox in the same account."""
        return self._transfer(reference, destination, "copy")

    def email_trash_message(self, reference: MessageRef) -> dict:
        """Move a message to the configured or discovered Trash mailbox."""
        target = self._mailbox(reference.account_id, "trash")
        if target == reference.mailbox:
            raise _invalid("The message is already in Trash.")
        return self._transfer(reference, target, "move")

    def _flags(
        self, reference: MessageRef, flags: list[str], operation: Literal["add", "remove"]
    ) -> dict:
        if not flags or any(flag not in SAFE_FLAGS for flag in flags):
            raise _invalid("Only seen, answered, flagged and draft flags are supported.")
        mailbox = self._check(reference)
        args = ["flag", operation, f"--mailbox={mailbox}"]
        args.extend(f"--flag={flag}" for flag in flags)
        self._run(
            reference.account_id,
            [*args, "--", reference.id],
            mutation=True,
            expected_uid_validity=reference.uid_validity,
        )
        return {"flags": flags}

    def email_add_flags(self, reference: MessageRef, flags: list[str]) -> dict:
        """Add safe message flags."""
        return self._flags(reference, flags, "add")

    def email_remove_flags(self, reference: MessageRef, flags: list[str]) -> dict:
        """Remove safe message flags."""
        return self._flags(reference, flags, "remove")

    def email_list_attachments(self, reference: MessageRef) -> dict:
        """List attachments, including inline parts, without marking the message read."""
        parts = _mime_parts(self._raw_message(reference))
        return {
            "attachments": [{k: v for k, v in item.items() if k != "content"} for item in parts]
        }

    def email_get_attachment(self, reference: MessageRef, attachment_id: str) -> dict:
        """Get one attachment as base64; host paths are never returned."""
        _text(attachment_id, "attachment ID")
        matches = [
            row for row in _mime_parts(self._raw_message(reference)) if row["id"] == attachment_id
        ]
        if len(matches) != 1:
            raise ProviderError("attachment_not_found", "The attachment was not found.")
        attachment = matches[0]
        if attachment["size"] > MAX_ATTACHMENT:
            raise _invalid("The attachment exceeds the 10 MiB limit.")
        with tempfile.TemporaryDirectory(prefix="attachment-", dir=self._temp.name) as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "content"
            path.write_bytes(attachment.pop("content"))
            content = path.read_bytes()
            return {"attachment": {**attachment, "base64": b64.b64encode(content).decode("ascii")}}

    def register_tools(self, mcp):
        for fn in (
            self.email_list_accounts,
            self.email_list_mailboxes,
            self.email_list_messages,
            self.email_search_messages,
            self.email_read_message,
            self.email_list_attachments,
            self.email_get_attachment,
        ):
            register_tool(mcp, fn, read_only=True, idempotent=True)
        for fn in (
            self.email_send_message,
            self.email_reply_message,
            self.email_forward_message,
            self.email_move_message,
            self.email_copy_message,
            self.email_trash_message,
            self.email_add_flags,
            self.email_remove_flags,
        ):
            register_tool(
                mcp, fn, destructive=fn in (self.email_move_message, self.email_trash_message)
            )
