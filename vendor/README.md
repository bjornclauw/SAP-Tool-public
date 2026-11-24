# Bundled runtime dependencies

`wheels/` contains the offline wheels used by the internal updater for portable
CPython 3.11 on Windows AMD64. `requirements.lock` pins exact versions and
SHA-256 hashes. The updater validates those hashes and extracts the wheels
directly into the portable Python; pip is not used and nothing is fetched from
the network.

Keep old wheels when adding a new version so Revert can restore the previous
lock. Only `py3-none-any` and `cp311-cp311-win_amd64` wheels are allowed.

Package license texts are in `licenses/`.
