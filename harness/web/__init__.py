"""Zero-build web UI assets served by ``harness.server``.

The directory is also a package so wheel builds carry the static files; the
server reads them from this directory and never mounts them outside auth.
"""
