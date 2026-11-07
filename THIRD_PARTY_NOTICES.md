# Third-party software

This service builds [Himalaya 2.2.1](https://github.com/pimalaya/himalaya/tree/v2.2.1) with the local UIDVALIDITY guard described in `patches/README.md` in the source distribution. Himalaya is distributed under the [MIT](https://github.com/pimalaya/himalaya/blob/v2.2.1/LICENSE-MIT) or [Apache 2.0](https://github.com/pimalaya/himalaya/blob/v2.2.1/LICENSE-APACHE) license. The binary includes third-party components subject to their own licenses; see the upstream repository and release materials.

Python dependencies and their versions are recorded in `uv.lock`; their respective distributions contain their license terms. Python and the container base image also have their own licenses.
