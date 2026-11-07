# Himalaya UIDVALIDITY guard

`himalaya-uidvalidity.patch` applies to the checksum-pinned Himalaya v2.2.1 source archive. It adds `--expected-uid-validity=<positive u32>` and checks the UIDVALIDITY returned by the IMAP SELECT on the same connection before an operation uses mailbox UIDs. A mismatch or missing value returns exit code 3; other failures retain upstream behavior. The guard applies to the IMAP paths used by himacal, not arbitrary raw IMAP commands or `--no-select` usage.

The Docker build uses the upstream `Cargo.lock` and default features. The unmodified release binary is included only in the test target as a negative-control fixture.
