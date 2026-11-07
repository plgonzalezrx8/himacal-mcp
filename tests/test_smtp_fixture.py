import importlib.util
from pathlib import Path


def test_greenmail_greeting_normalization_preserves_other_protocol_responses():
    path = Path(__file__).parent / "integration" / "smtp_greeting.py"
    spec = importlib.util.spec_from_file_location("smtp_greeting", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (
        module.normalize_greeting(b"220 /172.18.0.2 GreenMail SMTP ready\r\n")
        == b"220 greenmail.test GreenMail SMTP ready\r\n"
    )
    assert module.normalize_greeting(b"250-greenmail/172.18.0.2\r\n") == b"250-greenmail.test\r\n"
    for response in (
        b"250-AUTH PLAIN LOGIN\r\n",
        b"334 YWJjL2RlZg==\r\n",
        b"250 OK\r\n",
        b"220 smtp.example.test ready\r\n",
    ):
        assert module.normalize_greeting(response) == response
