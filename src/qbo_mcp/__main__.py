"""Command-line entry point: ``qbo-mcp authorize`` and ``qbo-mcp serve``."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qbo-mcp", description="QuickBooks Online time & invoice MCP server.")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("authorize", help="Connect a QuickBooks company: opens the Intuit consent page and stores tokens locally.")
    auth.add_argument("--no-browser", action="store_true", help="Print the consent URL instead of opening a browser.")
    auth.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the callback (default 300).")

    sub.add_parser("serve", help="Run the MCP server over stdio (used by Claude Code).")

    args = parser.parse_args(argv)

    if args.command == "authorize":
        from .auth import TokenStore, run_authorization_flow
        from .config import ConfigError, Settings

        try:
            settings = Settings.load()
            run_authorization_flow(settings, TokenStore(settings.token_path), open_browser=not args.no_browser, timeout=args.timeout)
        except (ConfigError, RuntimeError, TimeoutError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    from .server import serve

    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
