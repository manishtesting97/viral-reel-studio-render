"""Fail-closed Linux checks for the offline, sandboxed Chrome renderer."""

import os
from pathlib import Path
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep


def process_status(path):
    return dict(line.split(":\t", 1) for line in path.read_text().splitlines() if ":\t" in line)


def verify_renderer(browser, renderer):
    if len(renderer["NSpid"].split()) <= len(browser["NSpid"].split()):
        raise RuntimeError("Chrome renderer has no inner PID namespace.")
    if renderer["Seccomp"] != "2" or int(renderer["Seccomp_filters"]) <= int(browser["Seccomp_filters"]):
        raise RuntimeError("Chrome renderer has no additional seccomp filter.")


def chrome_renderers(browser_pid, proc=Path("/proc")):
    """Return live renderer processes descended from the Chrome executable."""
    parents = {}
    renderers = {}
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            status = process_status(process / "status")
            parents[int(process.name)] = int(status["PPid"])
            args = (process / "cmdline").read_bytes().split(b"\x00")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if b"--type=renderer" in args:
            renderers[int(process.name)] = status
    for pid, status in renderers.items():
        seen = set()
        while pid in parents and pid not in seen:
            seen.add(pid)
            pid = parents[pid]
            if pid == browser_pid:
                yield status
                break


def main():
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Network namespace contains a non-loopback interface.")
    if {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ENV", "VRS_GITHUB_PRODUCTION_TOKEN"} & os.environ.keys():
        raise RuntimeError("Unexpected GitHub environment inside render sandbox.")

    executed = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<script>fetch('/executed')</script>")
            else:
                self.send_response(204)
                self.end_headers()
                if self.path == "/executed":
                    executed.set()

        def log_message(self, _format, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http, TemporaryDirectory() as profile:
        Thread(target=http.serve_forever, daemon=True).start()
        try:
            with subprocess.Popen([
                "/tools/vrs-chrome", "--headless", "--disable-dev-shm-usage", "--no-first-run",
                f"--user-data-dir={profile}", f"http://127.0.0.1:{http.server_port}/",
            ], stdout=subprocess.DEVNULL) as chrome:
                try:
                    deadline = monotonic() + 30
                    while not executed.wait(0.1):
                        if chrome.poll() is not None or monotonic() >= deadline:
                            raise RuntimeError("Chrome did not execute the loopback fixture.")
                    browser = process_status(Path(f"/proc/{chrome.pid}/status"))
                    renderers = list(chrome_renderers(chrome.pid))
                    while not renderers and monotonic() < deadline and chrome.poll() is None:
                        sleep(0.1)
                        renderers = list(chrome_renderers(chrome.pid))
                    if not renderers:
                        raise RuntimeError("No live Chrome renderer was observable.")
                    for renderer in renderers:
                        verify_renderer(browser, renderer)
                finally:
                    if chrome.poll() is None:
                        chrome.terminate()
                        try:
                            chrome.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            chrome.kill()
        finally:
            http.shutdown()
    subprocess.run(["/tools/ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL)
    print("Loopback-only network, clean environment, Chrome renderer PID namespace and extra seccomp filter verified.")


if __name__ == "__main__":
    main()
