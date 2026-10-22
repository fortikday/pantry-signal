"""Pantry Signal: two independent local services for a restaurant demonstration.

Run python app.py. Orders are committed with a durable event log before the
inventory worker sees them. The worker uses HTTP and its own SQLite database.
This is a local prototype, not a hosted or authenticated restaurant system.
"""
from __future__ import annotations

import argparse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from domain import InventoryStore, OrderStore

ROOT = Path(__file__).resolve().parent
MAX_BODY = 65536


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("A JSON field was supplied more than once.")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Use finite numbers in JSON.")


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("This prototype connects only to a local HTTP service.")
    port = parsed.port
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Provide a local service port.")
    return f"http://{parsed.hostname}:{port}"


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class InventoryWorker:
    """Read committed source events; replay is safe after a process restart."""

    def __init__(self, store, source_url, interval=0.5):
        self.store = store
        self.source_url = local_url(source_url)
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None

    def tick(self):
        state = self.store.state()["sync"]
        if state["paused"]:
            return
        request = Request(
            f"{self.source_url}/api/events?after={state['last_event_id']}&limit=100",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=2) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError("The source event response was too large.")
            result = json.loads(body, object_pairs_hook=strict_object,
                                parse_constant=invalid_constant)
            if not isinstance(result, dict) or not isinstance(result.get("events"), list):
                raise ValueError("The order service returned invalid events.")
            if (not isinstance(result.get("stream_id"), str)
                    or type(result.get("latest_event_id")) is not int):
                raise ValueError("The source event stream identity is missing.")
            self.store.apply_events(result["events"], stream_id=result["stream_id"],
                                    latest_event_id=result["latest_event_id"])
            self.store.set_sync_error(None)
        except ValueError:
            self.store.set_sync_error(
                "The order history could not be matched safely. Keep both original logs and review the source."
            )
        except (URLError, OSError, KeyError, sqlite3.Error):
            self.store.set_sync_error(
                "Inventory is waiting for the order service. Saved orders will be retried."
            )

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    # Keep the HTTP server and later retries alive after a failed tick.
                    try:
                        self.store.set_sync_error("Inventory sync needs attention. Saved orders remain queued.")
                    except Exception:
                        # An unavailable database may also prevent the status write.
                        # Retrying later must not depend on that write succeeding.
                        pass
                self.stop_event.wait(self.interval)
        self.thread = threading.Thread(target=loop, name="inventory-event-reader", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=4)


def handler_factory(service, store, *, inventory_url=None, source_url=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PantrySignal/0.1"

        def log_message(self, fmt, *args):
            if getattr(self.server, "verbose", False) and sys.stderr is not None:
                super().log_message(fmt, *args)

        def origins(self):
            port = self.server.server_port
            allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            if service == "inventory":
                source = urlsplit(source_url)
                allowed.update({f"http://127.0.0.1:{source.port}", f"http://localhost:{source.port}"})
            return allowed

        def local_request(self):
            port = self.server.server_port
            if self.headers.get("Host", "").lower() not in {
                f"127.0.0.1:{port}", f"localhost:{port}"
            }:
                self.respond(403, {"error": "Open the app using its local browser address."})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in self.origins():
                self.respond(403, {"error": "This local app does not accept requests from that page."})
                return False
            return True

        def respond(self, status, payload=None, content_type="application/json; charset=utf-8"):
            if payload is None:
                content = b""
            elif isinstance(payload, bytes):
                content = payload
            else:
                content = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            origin = self.headers.get("Origin")
            if service == "inventory" and origin in self.origins():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("Invalid request size.") from None
            if not 0 < length <= MAX_BODY:
                raise ValueError("The request is empty or too large.")
            raw = self.rfile.read(length)
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Send JSON from the app's form.")
            try:
                result = json.loads(raw.decode("utf-8"),
                                    object_pairs_hook=strict_object,
                                    parse_constant=invalid_constant)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("The request did not contain valid JSON.") from None
            if not isinstance(result, dict):
                raise ValueError("The request must contain a JSON object.")
            return result

        def dispatch(self):
            if not self.local_request():
                return
            route = urlsplit(self.path)
            path, method = route.path, self.command
            if method == "OPTIONS":
                self.send_response(204)
                origin = self.headers.get("Origin")
                if origin in self.origins():
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "GET":
                if path == "/favicon.ico":
                    self.respond(204)
                elif path == "/api/state":
                    self.respond(200, store.state())
                elif service == "orders" and path in {"/", "/index.html"}:
                    self.respond(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif service == "orders" and path == "/api/config":
                    self.respond(200, {"today": date.today().isoformat(), "inventory_url": inventory_url,
                                       "mode": "local-prototype", "live_ai": False})
                elif service == "orders" and path == "/api/events":
                    query = parse_qs(route.query)
                    try:
                        after = int(query.get("after", ["0"])[0])
                        limit = int(query.get("limit", ["100"])[0])
                    except ValueError:
                        raise ValueError("Event cursor and limit must be whole numbers.") from None
                    if after < 0 or not 1 <= limit <= 100:
                        raise ValueError("Invalid event cursor or limit.")
                    events = store.events(after, limit)
                    self.respond(200, {"events": events, **store.source_info()})
                else:
                    self.respond(404, {"error": "This page was not found."})
                return
            if service == "orders":
                if method == "POST" and path == "/api/orders":
                    self.respond(201, {"order": store.create_order(self.body())})
                elif method == "POST" and path.startswith("/api/orders/") and path.endswith("/status"):
                    parts = path.split("/")
                    if len(parts) != 5 or not parts[3].isdigit():
                        raise ValueError("Choose an existing order.")
                    payload = self.body()
                    if set(payload) != {"status"}:
                        raise ValueError("Supply only the new order status.")
                    self.respond(200, {"order": store.transition_order(int(parts[3]), payload["status"])})
                elif method == "PUT" and path == "/api/recipes":
                    self.respond(200, {"recipe": store.save_recipe(self.body())})
                else:
                    self.respond(404, {"error": "This action was not found."})
            else:
                actions = {"/api/receipts": store.receive, "/api/waste": store.waste,
                           "/api/counts": store.count}
                if method == "POST" and path in actions:
                    self.respond(201, {"result": actions[path](self.body())})
                elif method == "POST" and path == "/api/demo-seed":
                    if self.body():
                        raise ValueError("The sample-data request must be empty.")
                    self.respond(201, {"result": store.seed_demo()})
                elif method == "POST" and path == "/api/sync":
                    payload = self.body()
                    if set(payload) != {"paused"} or not isinstance(payload["paused"], bool):
                        raise ValueError("Choose whether inventory sync is paused.")
                    store.set_paused(payload["paused"])
                    self.respond(200, {"sync": store.state()["sync"]})
                else:
                    self.respond(404, {"error": "This action was not found."})

        def handle_action(self):
            try:
                self.dispatch()
            except ValueError as exc:
                self.respond(400, {"error": str(exc)})
            except sqlite3.Error:
                self.respond(503, {"error": "The local log is busy. Retry the same action in a moment."})
            except Exception:
                self.respond(500, {"error": "The app could not complete this action. Check the local service."})

        do_GET = handle_action
        do_POST = handle_action
        do_PUT = handle_action
        do_OPTIONS = handle_action
    return Handler


def make_orders_server(data_dir, port=8770, inventory_url="http://127.0.0.1:8771"):
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    store = OrderStore(folder / "orders.sqlite3")
    return LocalServer(("127.0.0.1", port), handler_factory("orders", store,
                                                          inventory_url=local_url(inventory_url)))


def make_inventory_server(data_dir, port=8771, source_url="http://127.0.0.1:8770"):
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    store = InventoryStore(folder / "inventory.sqlite3")
    server = LocalServer(("127.0.0.1", port), handler_factory("inventory", store,
                                                          source_url=local_url(source_url)))
    server.inventory_worker = InventoryWorker(store, source_url)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "local")
    parser.add_argument("--orders-port", type=int, default=8770)
    parser.add_argument("--inventory-port", type=int, default=8771)
    parser.add_argument("--source-url", default="http://127.0.0.1:8770")
    parser.add_argument("--orders-only", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if args.orders_only and args.inventory_only:
        parser.error("Choose only one service mode.")
    if not all(1 <= p <= 65535 for p in (args.orders_port, args.inventory_port)):
        parser.error("Ports must be between 1 and 65535.")
    if not args.inventory_only and args.orders_port == args.inventory_port:
        parser.error("Use a different port for each service.")
    child = None
    server = None
    try:
        if args.inventory_only:
            server = make_inventory_server(args.data_dir, args.inventory_port, args.source_url)
            server.inventory_worker.start()
            print(f"Inventory service: http://127.0.0.1:{server.server_port}", flush=True)
        else:
            inventory_url = f"http://127.0.0.1:{args.inventory_port}"
            server = make_orders_server(args.data_dir, args.orders_port, inventory_url)
            address = f"http://127.0.0.1:{server.server_port}"
            if not args.orders_only:
                command = [sys.executable, str(ROOT / "app.py"), "--inventory-only",
                           "--inventory-port", str(args.inventory_port), "--source-url", address,
                           "--data-dir", str(args.data_dir.resolve())]
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
                child = subprocess.Popen(command, **options)
                print(f"Inventory service process: {child.pid}", flush=True)
            print(f"Pantry Signal: {address}", flush=True)
            print("Local demonstration. Keep this window open. Ctrl+C stops both services.", flush=True)
            if not args.no_browser:
                webbrowser.open(address)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"Pantry Signal could not start: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            worker = getattr(server, "inventory_worker", None)
            if worker is not None:
                worker.stop()
            server.server_close()
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
