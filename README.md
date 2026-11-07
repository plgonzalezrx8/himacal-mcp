# Himacal-MCP

![Himacal-MCP banner with a mountain, envelope and calendar emblem](assets/himacal-banner.png)

Himacal-MCP gives a trusted MCP client access to configured email and CalDAV accounts over Streamable HTTP. It runs as a Docker service, using the Himalaya CLI for email and a CalDAV client for calendars.

**One bearer token grants access to every configured account.** Anyone with that token can read and change account data and send email. Use it only with clients you trust.

| Area | What you can do |
| --- | --- |
| Email | List mailboxes, search and read messages, send, reply, forward, move, copy, trash, change supported flags, and work with attachments. Reading does not mark messages seen. |
| Calendar | List calendars, search events, and create, update or delete timed and all-day events, recurring series and individual occurrences. |
| Accounts | Configure named email and/or CalDAV accounts, each with its own provider credentials. |

## Requirements

- A Linux host with Git, OpenSSL, Docker Engine and Docker Compose. The supplied image targets **Linux x86-64** (`linux/amd64`); other host architectures need compatible emulation.
- An MCP client that supports Streamable HTTP and a custom `Authorization` header.
- Provider connection details and credentials for at least one email or CalDAV account. An app-specific password may be required.

Python is not needed for normal Docker use. Building the image downloads the pinned Himalaya source and dependencies and can take some time.

## Setup

The steps below use a Linux shell. An optional PowerShell alternative for copying the configuration and generating the token follows the connection instructions.

### 1. Get the project

```sh
git clone https://github.com/plgonzalezrx8/himacal-mcp.git
cd himacal-mcp
```

Copy the example configuration:

```sh
cp himacal.example.toml himacal.toml
```

Edit `himacal.toml` for your provider. The example has a named `personal` account with independently optional `email` and `caldav` sections; keep at least one. Use your provider's actual mailbox names, server addresses and usernames. Email settings use the native [Himalaya 2.2.1 configuration](https://github.com/pimalaya/himalaya/blob/v2.2.1/config.sample.toml); CalDAV uses `url`, `username`, `password_file` and an IANA `timezone`.

The example reads the same provider secret for IMAP, SMTP and CalDAV. Do not put passwords in TOML. Treat the configuration as trusted: Himalaya password commands are executed by the CLI. The example commands only read the mounted secret file.

### 2. Create secrets

Create `.secrets/mcp_password` as a new, independent high-entropy token. It must be a single line of at least 32 printable ASCII characters without spaces. These commands write a random token directly to a new file without displaying it and refuse to overwrite an existing file.

```sh
mkdir -p .secrets
(umask 077; set -o noclobber; openssl rand -hex 32 > .secrets/mcp_password)
```

Create `.secrets/provider_password` manually with your provider password or app-specific password as its only line. Use a private editor that does not leave unprotected backup files; do not enter the credential in a shell command or commit either file. `himacal.toml`, `.secrets/` and `.env` are ignored by Git, but remain sensitive on your machine.

The container runs as UID 10001 and must be able to read both files. On a native Linux Docker host, file-backed Compose secrets are bind mounts and may ignore Compose ownership and mode settings. After creating the files, restrict them while allowing that UID to read them:

```sh
chmod 0711 .secrets
sudo chown 10001:10001 .secrets/mcp_password .secrets/provider_password
sudo chmod 0400 .secrets/mcp_password .secrets/provider_password
```

With Docker Desktop or rootless/user-namespaced Docker, host-to-container ownership mapping differs. Restrict access using the host's controls and verify the container can read the files without exposing them to other users.

### 3. Start and connect

```sh
docker compose up -d --build
docker compose ps
```

The root Compose service binds `127.0.0.1:8000` by default and sets `HIMACAL_PUBLIC_URL` to `http://localhost:8000`. Configure your MCP client with:

```text
Transport: Streamable HTTP
URL:       http://localhost:8000/mcp
Header:    Authorization: Bearer <contents of .secrets/mcp_password>
```

Enter the token through your client's secure configuration. Do not put it in shared logs or command history. Client configuration formats differ; a client must support custom headers to use this endpoint.

To use another host port, set `HIMACAL_HOST_PORT` and `HIMACAL_PUBLIC_URL` together in your shell or a local `.env` file, for example `8001` and `http://localhost:8001`. The public URL is the origin only, without `/mcp`. Recreate the service after changing configuration:

```sh
docker compose up -d --force-recreate
```

<details>
<summary>Optional: PowerShell setup</summary>

After cloning the repository, use these commands in its directory instead of the Linux configuration-copy and token-generation commands above. The token file is created without displaying the token or overwriting an existing file.

```powershell
Copy-Item himacal.example.toml himacal.toml
New-Item -ItemType Directory -Force .secrets | Out-Null
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$token = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
$path = Join-Path (Get-Location).Path '.secrets/mcp_password'
$file = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
try {
    $encoded = [Text.Encoding]::ASCII.GetBytes($token)
    $file.Write($encoded, 0, $encoded.Length)
} finally { $file.Dispose() }
```

Edit the configuration, create the provider secret manually, and restrict both secrets as described above. Docker Desktop's host-to-container ownership mapping differs from native Linux; use equivalent restricted access and verify UID 10001 can read the secrets. Then start the service and configure your MCP client as above.

</details>

### More accounts or separate credentials

Add another named section, such as `[accounts.work.email]` or `[accounts.work.caldav]`, following the example. Account names identify the account in tool calls; use 1–100 ASCII letters, digits, `_` or `-`. Each account can have either or both services.

For a different provider credential, create another private file, add it to the service's `secrets` list **and** the top-level `secrets` mapping in `docker-compose.yml`, then point that account's Himalaya password commands and/or CalDAV `password_file` at its `/run/secrets/` path. For example, the Compose additions for `.secrets/work_password` are:

```yaml
services:
  himacal:
    secrets:
      - mcp_password
      - provider_password
      - work_password

secrets:
  work_password:
    file: ./.secrets/work_password
```

Keep the existing top-level `mcp_password` and `provider_password` mappings. Set the applicable command to `cat /run/secrets/work_password` and the CalDAV `password_file` to `/run/secrets/work_password`. Restrict the new file and ensure UID 10001 can read it as described above. Recreate the service after changes.

### iCloud

Live iCloud compatibility has **not** been verified. [Apple's mail settings](https://support.apple.com/en-us/102525) list IMAP `imap.mail.me.com:993` and SMTP `smtp.mail.me.com:587`, with encrypted connections required. This starting configuration uses TLS for IMAP and STARTTLS for SMTP. Replace the corresponding settings in the example email section:

```toml
imap.server = "imaps://imap.mail.me.com:993"
imap.sasl.plain.username = "YOUR_ICLOUD_MAIL_NAME"
smtp.server = "smtp://smtp.mail.me.com:587"
smtp.starttls = true
smtp.sasl.plain.username = "YOUR_FULL_ICLOUD_MAIL_ADDRESS"
```

Retain the password commands from the example and replace all placeholders, including the account's `email` address. Apple says the IMAP username is usually the address's name portion, with the full address as a fallback; SMTP uses the full address. Use an [app-specific password](https://support.apple.com/en-us/102654) in the provider secret file.

For calendars, the [CalDAV library documents](https://caldav.readthedocs.io/stable/about.html#some-notes-on-caldav-urls) `https://caldav.icloud.com/` as the discovery endpoint. Replace the example CalDAV section with your account details:

```toml
[accounts.personal.caldav]
url = "https://caldav.icloud.com/"
username = "YOUR_APPLE_ACCOUNT_EMAIL"
password_file = "/run/secrets/provider_password"
timezone = "America/New_York"
```

Choose your own IANA timezone. Himacal discovers calendars from the endpoint; tools use the discovered calendar identifiers. This configuration is a starting point, not evidence of a successful live iCloud test.

## Operations

- **Status:** `docker compose ps` shows the service and its health.
- **Stop:** `docker compose down` stops and removes the service container; host configuration and secrets remain. Start it again with `docker compose up -d`.
- **Restart:** `docker compose restart himacal` restarts the running container. After editing TOML, Compose settings, environment variables or provider credentials, use `docker compose up -d --force-recreate` so the service reads the changes.
- **Rotate/revoke the MCP token:** replace `.secrets/mcp_password` with a new independent token, preserve restricted permissions, and recreate the service. Update all clients; the previous token is rejected after recreation. To revoke access for one client without granting it the replacement, rotate the shared token and distribute the new one only to clients that should retain access.
- **Remove an account:** remove its TOML sections and any now-unused entry in the service's Compose `secrets` list and top-level mapping, then recreate the service. Handle the old credential file according to your host's practices. If necessary, revoke the provider credential with the provider too.

## Remote access

For remote clients, terminate HTTPS at a trusted reverse proxy and set `HIMACAL_PUBLIC_URL` to the exact external HTTPS origin, such as `https://mcp.example.com`, with no path. Forward the original `Host`, `Origin` and `Authorization` headers; do not use wildcard host or origin rules. Disable response buffering for streaming/SSE and allow long-lived connections. The application does not provide TLS itself.

Keep the service bound to loopback when the proxy runs on the host. If the proxy runs elsewhere, deliberately configure the binding and network access so only the trusted proxy can reach it. The server validates the host and, when present, origin against the configured public URL and local defaults.

## Safety and limitations

- Sending email and deleting a complete calendar event have no reliable undo. Trash can be restored by moving a message back; a cancelled calendar occurrence can be restored by an update. If a write reports an unknown outcome, inspect the provider before trying again. Mutations are not automatically retried.
- Message references include IMAP UIDVALIDITY. The bundled CLI checks it when selecting the mailbox on the **same connection** that reads or changes the message; missing or changed values reject the reference before its UID is used. Listing and searching use the same guard. Disconnects fail without replay. The image builds Himalaya v2.2.1 with the [UIDVALIDITY guard patch](patches/README.md); startup with an email account rejects an unpatched CLI.
- Sending and forwarding attachments is limited to 10 MiB each and 25 MiB total; downloading an attachment is limited to 10 MiB. Raw messages are limited to 64 MiB, 1,000 MIME parts and 32 nesting levels. Attached email (`message/rfc822`, often `.eml`) containing non-ASCII bytes or non-CRLF line endings is rejected before forwarding because the pinned CLI cannot preserve those bytes through its raw-message input.
- HTTP request bodies are limited to 40 MiB, including JSON and base64 overhead.
- Calendar updates and deletes require a current strong ETag. All-day end dates are exclusive. Timezone changes require both start and end; generated IANA timezone definitions cover at least 100 years beyond the event. Converting a recurring series between timed and all-day dates is rejected if existing exceptions would become incompatible. Invitations, RSVP and scheduling-triggering writes are excluded.
- Calendar searches cover at most 366 days and return at most 1,000 instances. Expansion is capped at 10,000 recurrence candidates, including history, so unusually old or frequent series can exceed the bound.

## Development and verification

With Python 3.13 and [uv](https://docs.astral.sh/uv/), install dependencies and run the local checks:

```sh
uv sync --frozen --python 3.13
uv run pytest -q
uv run ruff check src tests
```

Run provider acceptance with Docker and `uv run python tests/integration/run.py`. It builds the root Compose service and test image, exercises two disposable GreenMail/Radicale accounts, and checks the packaged service, restart and token rotation. It uses its own temporary Compose project and test secrets; it does not read your local `himacal.toml` or connect to your live accounts. The test-only SMTP relay corrects GreenMail's nonstandard greeting hostname; authentication, SMTP commands and message bytes pass through unchanged, and the relay is not in the production image.

Email workflows have been tested against GreenMail, calendar workflows against Radicale, and the HTTP interface with current and legacy MCP clients. Acceptance also checks mailbox reset and disconnect races against a local IMAP fixture. Live iCloud and other providers have not been verified. Local Python development with email accounts requires the guarded Himalaya build; the container acceptance command builds it.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Startup rejects configuration or a secret | Check TOML syntax, provider settings, absolute container secret paths and UID 10001's read access. The MCP token must meet the requirements above. |
| HTTP `401` | Supply exactly one `Authorization: Bearer …` header with the current token. Recreate the service and update clients after rotation. |
| HTTP `403` or `421` | Check the request's `Origin` or `Host` against `HIMACAL_PUBLIC_URL`, including its port, and check proxy header forwarding. |
| `stale_reference` or calendar `conflict` | List or retrieve the affected object again and use the fresh reference or version. |
| `outcome_unknown` | Check the provider before retrying: the operation may already have completed. |

## License

Himacal-MCP is available under the [MIT License](LICENSE). Third-party components retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
