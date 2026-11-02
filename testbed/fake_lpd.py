"""Minimal RFC 1179 receiver that accepts jobs like the MG5350's LPD queue."""
from __future__ import annotations

import os
import socketserver
import sys
import time

SPOOL = os.environ.get("FAKE_LPD_SPOOL", "/var/spool/fake-lpd")


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        command = self.rfile.readline()
        self.wfile.write(b"\0")
        if not command or command[0] != 2:  # only "receive a printer job"
            return
        os.makedirs(SPOOL, exist_ok=True)
        while True:
            subcommand = self.rfile.readline()
            if not subcommand or subcommand[0] == 1:
                self.wfile.write(b"\0")
                return
            size, name = subcommand[1:].decode().strip().split(" ", 1)
            self.wfile.write(b"\0")
            remaining = int(size)
            with open(os.path.join(SPOOL, f"{time.time_ns()}-{name}"), "wb") as output:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        return
                    output.write(chunk)
                    remaining -= len(chunk)
            self.rfile.read(1)  # trailing zero byte
            self.wfile.write(b"\0")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Server(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 515), Handler).serve_forever()
