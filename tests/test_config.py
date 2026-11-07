import pytest

from himacal.config import load_settings


@pytest.fixture
def configured(tmp_path, monkeypatch):
    password_file = tmp_path / "mcp-secret"
    password_file.write_text("test-access-password-with-more-than-32-characters\n")
    provider_file = tmp_path / "provider-secret"
    provider_file.write_text("test-provider-password\n")
    config = tmp_path / "himacal.toml"
    config.write_text("""
[accounts.personal.email]
email = "user@example.test"
imap.server = "imap://localhost:3143"
imap.sasl.plain.username = "user"
imap.sasl.plain.password.command = ["cat", "/run/secrets/email_password"]
mailbox.alias.trash = "Trash"

[accounts.personal.caldav]
url = "http://localhost:5232"
username = "user"
password_file = "provider-secret"
timezone = "America/New_York"
""")
    monkeypatch.setenv("HIMACAL_CONFIG", str(config))
    monkeypatch.setenv("HIMACAL_MCP_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("HIMACAL_PUBLIC_URL", "https://mcp.example.test")
    return config, password_file, provider_file


def test_load_preserves_native_email_and_resolves_calendar_secret(configured):
    _config, _, provider_file = configured
    settings = load_settings()
    account = settings.accounts["personal"]
    assert account.email["imap"]["sasl"]["plain"]["password"]["command"] == [
        "cat",
        "/run/secrets/email_password",
    ]
    assert account.email["mailbox"]["alias"]["trash"] == "Trash"
    assert account.caldav.password_file == provider_file
    assert account.caldav.timezone == "America/New_York"
    assert settings.public_url == "https://mcp.example.test"
    assert "test-access-password" not in repr(settings)


@pytest.mark.parametrize("replacement", ["", "short", "replace-me", "x" * 40 + "\ninjected"])
def test_refuses_missing_weak_or_multiline_access_secret(configured, replacement):
    _, password_file, _ = configured
    password_file.write_text(replacement)
    with pytest.raises(ValueError, match="MCP password"):
        load_settings()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[accounts.empty]\n",
        '[accounts.bad.caldav]\nurl="https://example.test"\n',
        '[accounts.mail.email]\nunknown_backend = "host"\n',
    ],
)
def test_rejects_incomplete_configuration(configured, content):
    config, _, _ = configured
    config.write_text(content)
    with pytest.raises(ValueError, match="configuration"):
        load_settings()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host",
        "http://remote.example.test",
        "https://user:secret@host",
        "https://host/mcp",
        "https://host?password=secret",
    ],
)
def test_rejects_unsafe_public_url(configured, monkeypatch, url):
    monkeypatch.setenv("HIMACAL_PUBLIC_URL", url)
    with pytest.raises(ValueError, match="PUBLIC_URL"):
        load_settings()


def test_bad_config_error_never_contains_password_values(configured):
    config, _, _ = configured
    config.write_text('[accounts.secret.caldav]\npassword_file={raw="sensitive-password"}\n')
    with pytest.raises(ValueError) as error:
        load_settings()
    assert "sensitive-password" not in str(error.value)


def test_unknown_account_does_not_fall_back(configured):
    settings = load_settings()
    from himacal.errors import ProviderError

    with pytest.raises(ProviderError) as error:
        settings.require_account("absent", "email")
    assert error.value.code == "account_not_found"
