import argparse
import asyncio
from typing import List, Optional

from dotenv import load_dotenv

from babas.bridge.protocol import DEFAULT_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="babas", description="BABAS: browser automation bot assistant system")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("cli", help="interactive console agent (default)")
    serve = subparsers.add_parser("serve", help="run the extension bridge server")
    serve.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default: {DEFAULT_PORT})")
    serve.add_argument("--repl", action="store_true", help="open the developer console on the connected extension")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    load_dotenv()
    if args.command == "serve":
        from babas.devrepl import run_serve

        try:
            asyncio.run(run_serve(args.host, args.port, args.repl))
        except KeyboardInterrupt:
            print("\nBridge stopped")
        return
    from babas.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
