# Frozen local native build inputs

`packages.json` contains only the 392 public crate names, versions, registry download URLs, expected SHA256 and archive lengths. It is projected from the verified dependency/cache inventory; package hashes and lengths are unchanged. It contains no local download receipts, source contents, credentials or diagnostic traces.

`native-build.json` freezes the reviewed build entry sources, local tool hashes and resource policy for macOS arm64. `${HOME}` is resolved to the current user's home before checking tool hashes. A different toolchain must be reviewed and explicitly re-frozen; the builder does not silently accept replacements. This input is not execution authorization.

The offline builder requires the existing verified archive cache at `~/Library/Caches/AIPlatform/f8-native-g1/000ff71a45fe`. The suffix is the historical G1 cache identifier; the current first-party root lock additionally lists serde_json and sha2 directly. No crate versions or package checksums changed. The builder does not download missing archives.

Generated resolution/build/verification reports remain in ignored g0/g1/g2 evidence directories. Only reusable source, tests, the public package inventory and the frozen build profile are tracked.
