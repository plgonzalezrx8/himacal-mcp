FROM rust:1.93.1-bookworm@sha256:7c4ae649a84014c467d79319bbf17ce2632ae8b8be123ac2fb2ea5be46823f31 AS himalaya-build
WORKDIR /usr/local/src/himalaya
COPY patches/himalaya-uidvalidity.patch /tmp/himalaya-uidvalidity.patch
RUN curl -fsSL -o /tmp/himalaya-source.tgz https://github.com/pimalaya/himalaya/archive/refs/tags/v2.2.1.tar.gz \
    && echo '261b6d3bce4f0de6399b347525fc3c687f309d83840b4e10a58ab7ad70c03544  /tmp/himalaya-source.tgz' | sha256sum -c - \
    && tar -xzf /tmp/himalaya-source.tgz --strip-components=1 \
    && patch -p1 -i /tmp/himalaya-uidvalidity.patch \
    && cargo build --locked --release

FROM python:3.13-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.17
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM build AS test
RUN uv sync --frozen --no-editable
RUN curl -fsSL -o /tmp/himalaya.tgz https://github.com/pimalaya/himalaya/releases/download/v2.2.1/himalaya.x86_64-linux.tgz \
    && echo '5c5ba2724c162f82d0a0c71b6c03224ed44f8bef7b14ace5da0635d3af2665a0  /tmp/himalaya.tgz' | sha256sum -c - \
    && tar -xzf /tmp/himalaya.tgz -C /usr/local/bin himalaya \
    && mv /usr/local/bin/himalaya /usr/local/bin/himalaya-unguarded-fixture \
    && rm /tmp/himalaya.tgz
COPY --from=himalaya-build /usr/local/src/himalaya/target/release/himalaya /usr/local/bin/himalaya
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp \
    HIMACAL_UNGUARDED_HIMALAYA=/usr/local/bin/himalaya-unguarded-fixture
CMD ["pytest"]

FROM python:3.13-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin himacal
COPY --from=himalaya-build /usr/local/src/himalaya/target/release/himalaya /usr/local/bin/himalaya
COPY --from=himalaya-build /usr/local/src/himalaya/LICENSE-MIT /usr/share/doc/himalaya/LICENSE-MIT
COPY --from=himalaya-build /usr/local/src/himalaya/LICENSE-APACHE /usr/share/doc/himalaya/LICENSE-APACHE
COPY --from=build /app /app
COPY LICENSE /usr/share/doc/himacal/LICENSE
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/himacal/THIRD_PARTY_NOTICES.md
ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp \
    HIMACAL_CONFIG=/etc/himacal/config.toml \
    HIMACAL_MCP_PASSWORD_FILE=/run/secrets/mcp_password \
    HIMACAL_PUBLIC_URL=http://localhost:8000
USER 10001:10001
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["himacal-mcp", "--healthcheck"]
CMD ["himacal-mcp"]
