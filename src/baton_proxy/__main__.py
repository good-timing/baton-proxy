"""Entry point for `python -m baton_proxy` and the `baton-proxy` console script."""

from __future__ import annotations

from baton_proxy.proxy import main

if __name__ == "__main__":
    raise SystemExit(main())
