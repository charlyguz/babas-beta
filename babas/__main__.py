import argparse
import asyncio
import errno
import sys
from dataclasses import replace
from typing import List, Optional

from babas.config import Settings
from babas.logs import setup_logging


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {text!r}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 0 and 65535, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="babas", description="BABAS: browser automation bot assistant system")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("cli", help="interactive console agent (default)")
    serve = subparsers.add_parser("serve", help="run the extension bridge server")
    serve.add_argument("--host", default=None, help="interface to listen on (default: BABAS_BRIDGE_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=_port, default=None, help="port to listen on (default: BABAS_BRIDGE_PORT or 8765)")
    serve.add_argument("--repl", action="store_true", help="open the developer console on the connected extension")
    serve.add_argument(
        "--mcp-http",
        action="store_true",
        help="also serve MCP over streamable HTTP on 127.0.0.1 (BABAS_MCP_HTTP_PORT, default 8766); needs babas[mcp]",
    )
    subparsers.add_parser("mcp", help="run the MCP server over stdio (needs babas[mcp])")
    pair = subparsers.add_parser("pair", help="show or reset the pairing with the browser extension")
    pair.add_argument("--reset", action="store_true", help="forget the pairing; the extension pairs again on its next connection")
    return parser


def run_pair(settings: Settings, reset: bool) -> None:
    from babas.bridge.server import reset_pairing

    path = settings.pairing_file
    if not reset:
        if path.exists():
            print(f"Pairing file exists: {path}")
        else:
            print(f"No pairing file: {path}")
            print("The extension pairs itself the first time it connects to 'python -m babas serve'.")
        return
    try:
        removed = reset_pairing(path)
    except OSError as exc:
        print(f"Could not remove the pairing file {path}: {exc.strerror or type(exc).__name__}")
        raise SystemExit(1) from None
    if removed:
        print("Pairing removed. The extension will pair again the next time it connects.")
    else:
        print("No pairing file.")


def run_mcp(settings: Settings) -> None:
    # stdout is the MCP protocol channel: everything here goes to stderr.
    setup_logging(settings.log_level)
    from babas.devrepl import MCP_EXTRA_HINT

    try:
        from babas.mcp_server import run_stdio

        asyncio.run(run_stdio(settings))
    except KeyboardInterrupt:
        return
    except ImportError as exc:
        if (exc.name or "mcp").split(".")[0] != "mcp":
            raise
        print(MCP_EXTRA_HINT, file=sys.stderr)
        raise SystemExit(1) from None
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(
            f"Port {settings.bridge_port} is in use (is `babas serve` running?). "
            "Stop it or use its HTTP endpoint: python -m babas serve --mcp-http",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "serve" and args.repl and args.mcp_http:
        print("babas serve: error: --mcp-http cannot be combined with --repl", file=sys.stderr)
        raise SystemExit(2)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.command == "serve":
        from babas.devrepl import run_serve

        overrides = {}
        if args.host is not None:
            overrides["bridge_host"] = args.host
        if args.port is not None:
            overrides["bridge_port"] = args.port
        try:
            asyncio.run(run_serve(replace(settings, **overrides), args.repl, args.mcp_http))
        except KeyboardInterrupt:
            print("\nBridge stopped")
        return
    if args.command == "mcp":
        run_mcp(settings)
        return
    if args.command == "pair":
        run_pair(settings, args.reset)
        return
    from babas.cli import main as cli_main

    cli_main(settings)


if __name__ == "__main__":
    main()
