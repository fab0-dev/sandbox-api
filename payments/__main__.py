"""Server entry point: python3 -m payments [--host HOST] [--port PORT] [--db PATH]."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from payments.api import make_server
from payments.service import PaymentService
from payments.store import PaymentStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_DB = "payments.db"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m payments", description="The payment service (test mode only).")
    parser.add_argument("--host", default=DEFAULT_HOST, help="address to listen on (default %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on (default %(default)s)")
    parser.add_argument(
        "--db",
        default=os.environ.get("PAYMENTS_DB", DEFAULT_DB),
        help="sqlite3 file for the payments, or :memory: (default $PAYMENTS_DB or %s)" % DEFAULT_DB,
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be from 0 to 65535")

    store = PaymentStore(args.db)
    server = make_server(PaymentService(store), args.host, args.port)
    host, port = server.server_address[:2]
    print("Payment service (test mode) on http://%s:%s, payments in %s" % (host, port, args.db), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
