import base64
import imaplib
import json
import os
import time
import uuid
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest

if os.environ.get("HIMACAL_INTEGRATION") != "1":
    pytest.skip("Run isolated provider acceptance", allow_module_level=True)

from himacal.email import AttachmentInput, EmailService, MessageRef
from himacal.errors import ProviderError

pytestmark = pytest.mark.integration


@pytest.fixture
def email_service(provider_settings):
    service = EmailService(provider_settings)
    yield service
    service.close()


def find_message(service, account, mailbox, subject):
    for _ in range(30):
        messages = service.email_list_messages(account, mailbox, page_size=100)["messages"]
        found = [message for message in messages if message["envelope"].get("subject") == subject]
        if found:
            return MessageRef.model_validate(found[-1]["reference"])
        time.sleep(0.2)
    raise AssertionError(f"Message was not delivered to disposable {account}/{mailbox}")


def imap_read(reference):
    with imaplib.IMAP4("greenmail", 3143) as mail:
        mail.login(reference.account_id, f"{reference.account_id}-fixture-password")
        mail.select(reference.mailbox)
        status, result = mail.uid("fetch", reference.id, "(BODY.PEEK[] FLAGS)")
        assert status == "OK"
        metadata, raw = next(item for item in result if isinstance(item, tuple))
        return BytesParser(policy=policy.default).parsebytes(raw), metadata


def test_send_read_reply_and_forward_preserve_exact_attachment_bytes(email_service):
    subject = f"attachment-acceptance-{uuid.uuid4().hex}"
    payloads = {"binary.bin": b"\x00\xff\xfe\x80exact-bytes", "legacy.txt": b"caf\xe9\r\n"}
    attachments = [
        AttachmentInput(
            filename=name,
            mime="text/plain" if name.endswith(".txt") else "application/octet-stream",
            base64=base64.b64encode(payload).decode(),
        )
        for name, payload in payloads.items()
    ]
    sent = email_service.email_send_message(
        "alpha", ["beta@localhost"], subject, "original body marker", attachments=attachments
    )
    assert sent["accepted"] is True
    reference = find_message(email_service, "beta", "INBOX", subject)
    assert reference.uid_validity is not None
    read = email_service.email_read_message(reference)
    assert "original body marker" in json.dumps(read["message"])
    original, metadata = imap_read(reference)
    assert b"\\Seen" not in metadata
    listed = email_service.email_list_attachments(reference)["attachments"]
    assert {item["filename"] for item in listed} == set(payloads)
    for item in listed:
        retrieved = email_service.email_get_attachment(reference, str(item["id"]))["attachment"]
        assert base64.b64decode(retrieved["base64"]) == payloads[item["filename"]]

    email_service.email_reply_message(reference, "reply body marker")
    reply_ref = find_message(email_service, "alpha", "INBOX", "Re: " + subject)
    reply, _ = imap_read(reply_ref)
    assert reply["In-Reply-To"] == original["Message-ID"]
    assert "reply body marker" in reply.get_body(preferencelist=("plain",)).get_content()

    email_service.email_forward_message(reference, ["alpha@localhost"], "forward body marker")
    forward_ref = find_message(email_service, "alpha", "INBOX", "Fwd: " + subject)
    forwarded, _ = imap_read(forward_ref)
    actual = {
        part.get_filename(): part.get_payload(decode=True) for part in forwarded.iter_attachments()
    }
    assert actual == payloads


def test_search_flags_copy_move_trash_restore_and_stale_reference(email_service):
    subject = f"organization-acceptance-{uuid.uuid4().hex}"
    email_service.email_send_message("alpha", ["beta@localhost"], subject, "organization body")
    reference = find_message(email_service, "beta", "INBOX", subject)
    matches = email_service.email_search_messages("beta", "INBOX", f"subject {subject}")["messages"]
    assert any(item["envelope"].get("subject") == subject for item in matches)
    email_service.email_add_flags(reference, ["seen", "flagged"])
    _, flags = imap_read(reference)
    assert b"\\Seen" in flags and b"\\Flagged" in flags
    email_service.email_remove_flags(reference, ["seen", "flagged"])
    _, flags = imap_read(reference)
    assert b"\\Seen" not in flags and b"\\Flagged" not in flags

    stale = reference.model_copy(update={"uid_validity": reference.uid_validity + 1})
    with pytest.raises(ProviderError) as rejected:
        email_service.email_move_message(stale, "Archive")
    assert rejected.value.code == "stale_reference"
    assert email_service.email_read_message(reference)["message"]

    email_service.email_copy_message(reference, "Copies")
    copied = find_message(email_service, "beta", "Copies", subject)
    assert copied.mailbox != reference.mailbox
    email_service.email_move_message(reference, "Archive")
    archived = find_message(email_service, "beta", "Archive", subject)
    with pytest.raises(ProviderError):
        email_service.email_read_message(reference)
    email_service.email_trash_message(archived)
    trashed = find_message(email_service, "beta", "Trash", subject)
    with pytest.raises(ProviderError):
        email_service.email_trash_message(trashed)
    email_service.email_move_message(trashed, "INBOX")
    restored = find_message(email_service, "beta", "INBOX", subject)
    assert "organization body" in json.dumps(email_service.email_read_message(restored)["message"])
    assert not any(
        item["envelope"].get("subject") == subject
        for item in email_service.email_list_messages("alpha", "INBOX")["messages"]
    )


def test_failed_sent_copy_reports_uncertain_outcome_without_resending(
    provider_settings, email_service
):
    accounts = {
        key: value.model_copy(deep=True) for key, value in provider_settings.accounts.items()
    }
    accounts["alpha"].email["message"]["send"]["save-copy"] = "MissingSentFolder"
    service = EmailService(replace(provider_settings, accounts=accounts))
    subject = f"uncertain-send-{uuid.uuid4().hex}"
    try:
        with pytest.raises(ProviderError) as failure:
            service.email_send_message(
                "alpha", ["beta@localhost"], subject, "accepted before save-copy failed"
            )
        assert failure.value.code == "outcome_unknown"
        find_message(email_service, "beta", "INBOX", subject)
        matches = email_service.email_search_messages("beta", "INBOX", f"subject {subject}")[
            "messages"
        ]
        assert len(matches) == 1
    finally:
        service.close()


def test_invalid_provider_credentials_are_redacted(provider_settings):
    accounts = {
        key: value.model_copy(deep=True) for key, value in provider_settings.accounts.items()
    }
    bad_password = "invalid-fixture-password-do-not-return"
    accounts["alpha"].email["imap"]["sasl"]["login"]["password"] = {"raw": bad_password}
    service = EmailService(replace(provider_settings, accounts=accounts))
    try:
        with pytest.raises(ProviderError) as failure:
            service.email_list_mailboxes("alpha")
        assert failure.value.code == "provider_error"
        assert bad_password not in str(failure.value)
    finally:
        service.close()
