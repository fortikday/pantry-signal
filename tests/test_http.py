"""Exercise the two local HTTP services with isolated, fictional inventory."""

from contextlib import contextmanager
from http.client import HTTPConnection, RemoteDisconnected
import json
from pathlib import Path
import sys
import threading
from urllib.error import URLError
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        worker = getattr(server, "inventory_worker", None)
        if worker is not None:
            worker.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(server, path, method="GET", payload=None, headers=None, raw=None):
    supplied = dict(headers or {})
    if payload is not None:
        raw = json.dumps(payload)
        supplied.setdefault("Content-Type", "application/json")
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=4)
    try:
        connection.request(method, path, body=raw, headers=supplied)
        response = connection.getresponse()
        body = response.read()
        result = json.loads(body) if body and "application/json" in response.getheader("Content-Type", "") else body
        return response.status, result, dict(response.getheaders())
    finally:
        connection.close()


@pytest.fixture
def pair(tmp_path):
    orders = app.make_orders_server(tmp_path, port=0,
                                   inventory_url="http://127.0.0.1:65534")
    inventory = app.make_inventory_server(
        tmp_path, port=0, source_url=f"http://127.0.0.1:{orders.server_port}")
    with running(orders), running(inventory):
        yield orders, inventory


def order_payload():
    return {"request_id": str(uuid4()), "table_number": 7,
            "lines": [{"menu_item_id": "caesar", "quantity": 1,
                       "modifiers": ["chicken", "shrimp"]}]}


def state(server):
    status, result, _ = request(server, "/api/state")
    assert status == 200, result
    return result


def ingredient(inventory, name):
    return next(row for row in state(inventory)["ingredients"] if row["id"] == name)


def create(orders, payload=None):
    status, result, _ = request(orders, "/api/orders", "POST", payload or order_payload())
    assert status == 201, result
    return result["order"]


def transition(orders, order_id, status):
    code, result, _ = request(orders, f"/api/orders/{order_id}/status", "POST", {"status": status})
    assert code == 200, result
    return result["order"]


def test_orders_and_kitchen_do_not_call_the_inventory_service(pair, monkeypatch):
    orders, _ = pair
    def forbidden(*args, **kwargs):
        raise AssertionError("Order handling tried to contact inventory")
    monkeypatch.setattr(app, "urlopen", forbidden)
    order = create(orders)
    assert order["status"] == "queued"
    assert transition(orders, order["id"], "preparing")["status"] == "preparing"
    assert transition(orders, order["id"], "ready")["status"] == "ready"
    assert len(state(orders)["orders"]) == 1


def test_http_request_id_retries_are_idempotent_and_conflicts_fail(pair):
    orders, _ = pair
    payload = order_payload()
    first, second = create(orders, payload), create(orders, payload)
    assert first["id"] == second["id"]
    assert state(orders)["latest_event_id"] == 1
    payload["table_number"] = 8
    status, error, _ = request(orders, "/api/orders", "POST", payload)
    assert status == 400 and error.get("error")
    assert len(state(orders)["orders"]) == 1


def test_inventory_catches_up_after_pause_without_duplicate_usage(pair):
    orders, inventory = pair
    assert request(inventory, "/api/demo-seed", "POST", {})[0] == 201
    before = ingredient(inventory, "chicken")["on_hand_g"]
    assert request(inventory, "/api/sync", "POST", {"paused": True})[0] == 200
    order = create(orders)
    transition(orders, order["id"], "preparing")
    transition(orders, order["id"], "ready")
    inventory.inventory_worker.tick()
    assert state(inventory)["sync"]["last_event_id"] == 0
    assert ingredient(inventory, "chicken")["on_hand_g"] == before
    assert request(inventory, "/api/sync", "POST", {"paused": False})[0] == 200
    inventory.inventory_worker.tick()
    chicken = ingredient(inventory, "chicken")
    assert chicken["on_hand_g"] == before - 400
    assert chicken["reserved_g"] == 0
    assert state(inventory)["sync"]["last_event_id"] == state(orders)["latest_event_id"]
    inventory.inventory_worker.tick()
    assert ingredient(inventory, "chicken")["on_hand_g"] == before - 400


def test_cancellation_http_preserves_only_already_consumed_ingredients(pair):
    orders, inventory = pair
    request(inventory, "/api/demo-seed", "POST", {})
    before = ingredient(inventory, "chicken")["on_hand_g"]
    uncooked = create(orders)
    inventory.inventory_worker.tick()
    assert ingredient(inventory, "chicken")["reserved_g"] == 400
    transition(orders, uncooked["id"], "cancelled")
    inventory.inventory_worker.tick()
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert ingredient(inventory, "chicken")["on_hand_g"] == before
    cooked = create(orders)
    transition(orders, cooked["id"], "preparing")
    transition(orders, cooked["id"], "cancelled")
    inventory.inventory_worker.tick()
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert ingredient(inventory, "chicken")["on_hand_g"] == before - 400


def test_source_unavailable_sets_safe_error_and_retry_recovers(pair, monkeypatch):
    orders, inventory = pair
    create(orders)
    real_open = app.urlopen
    def offline(*args, **kwargs):
        raise URLError("private source details should not leak")
    monkeypatch.setattr(app, "urlopen", offline)
    inventory.inventory_worker.tick()
    sync = state(inventory)["sync"]
    assert sync["last_event_id"] == 0
    assert sync["error"] and "private" not in sync["error"]
    monkeypatch.setattr(app, "urlopen", real_open)
    inventory.inventory_worker.tick()
    assert state(inventory)["sync"]["last_event_id"] == 1
    assert state(inventory)["sync"]["error"] is None


def test_hosts_and_foreign_origins_cannot_write_or_read_local_log(pair):
    orders, inventory = pair
    for server in (orders, inventory):
        assert request(server, "/api/state", headers={"Host": "evil.example"})[0] == 403
        assert request(server, "/api/state", headers={"Origin": "https://evil.example"})[0] == 403
    try:
        status, _, _ = request(orders, "/api/orders", "POST", order_payload(),
                               headers={"Origin": "https://evil.example"})
    except (ConnectionAbortedError, ConnectionResetError, RemoteDisconnected):
        # The local guard may close an unauthorized body without reading it.
        pass
    else:
        assert status == 403
    assert not state(orders)["orders"]


def test_inventory_cors_allows_only_the_configured_local_order_origin(pair):
    orders, inventory = pair
    origin = f"http://127.0.0.1:{orders.server_port}"
    status, _, headers = request(inventory, "/api/state", headers={"Origin": origin})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == origin
    status, _, headers = request(inventory, "/api/receipts", "OPTIONS",
                                 headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    assert status == 204 and headers["Access-Control-Allow-Origin"] == origin
    bad_origin = "http://127.0.0.1:65533"
    status, _, headers = request(inventory, "/api/receipts", "OPTIONS",
                                 headers={"Origin": bad_origin})
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("raw", [
    '{"table_number":7,"table_number":8}',
    '{"table_number":NaN}',
    '{"table_number":Infinity}',
    '[1,2,3]',
    '{broken',
])
def test_invalid_json_is_rejected_without_writes(pair, raw):
    orders, _ = pair
    status, result, _ = request(orders, "/api/orders", "POST", raw=raw,
                               headers={"Content-Type": "application/json"})
    assert status == 400 and result.get("error")
    assert not state(orders)["orders"]


def test_body_type_size_and_event_query_limits(pair):
    orders, _ = pair
    assert request(orders, "/api/orders", "POST", raw="{}",
                   headers={"Content-Type": "text/plain"})[0] == 400
    # Oversized bodies are rejected without reading unbounded input. Windows
    # can reset the connection when unread attacker-supplied bytes remain.
    try:
        status, _, _ = request(orders, "/api/orders", "POST", raw="x" * 65537,
                               headers={"Content-Type": "application/json"})
    except (ConnectionAbortedError, ConnectionResetError, RemoteDisconnected):
        pass
    else:
        assert status == 400
    for query in ("after=-1", "after=abc", "limit=0", "limit=101"):
        assert request(orders, f"/api/events?{query}")[0] == 400
    assert not state(orders)["orders"]


def test_inventory_starts_empty_and_seed_cannot_mix_into_an_active_log(pair):
    _, inventory = pair
    assert not state(inventory)["batches"]
    assert request(inventory, "/api/demo-seed", "POST", {"anything": True})[0] == 400
    assert request(inventory, "/api/demo-seed", "POST", {})[0] == 201
    batches = state(inventory)["batches"]
    assert batches
    assert request(inventory, "/api/demo-seed", "POST", {})[0] == 400
    assert state(inventory)["batches"] == batches


def test_changed_order_stream_is_reported_without_rewriting_inventory(pair, tmp_path):
    orders, inventory = pair
    create(orders)
    inventory.inventory_worker.tick()
    before = state(inventory)
    assert before["sync"]["last_event_id"] == 1
    other = app.make_orders_server(tmp_path / "different-restaurant", port=0,
                                   inventory_url="http://127.0.0.1:65534")
    with running(other):
        inventory.inventory_worker.source_url = f"http://127.0.0.1:{other.server_port}"
        inventory.inventory_worker.tick()
    after = state(inventory)
    assert after["sync"]["last_event_id"] == 1
    assert after["sync"]["error"]
    assert after["ingredients"] == before["ingredients"]
    inventory.inventory_worker.source_url = f"http://127.0.0.1:{orders.server_port}"
    inventory.inventory_worker.tick()
    assert state(inventory)["sync"]["error"] is None


def test_worker_retries_when_even_the_error_status_write_is_temporarily_unavailable():
    class UnavailableStatusStore:
        def set_sync_error(self, message):
            raise OSError("A transient local storage failure")

    worker = app.InventoryWorker(UnavailableStatusStore(), "http://127.0.0.1:65534", interval=0.01)
    recovered = threading.Event()
    calls = []
    def flaky_tick():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("The inventory read failed")
        recovered.set()
    worker.tick = flaky_tick
    worker.start()
    try:
        assert recovered.wait(timeout=1), "A failed status write terminated inventory retry processing"
        assert worker.thread.is_alive()
    finally:
        worker.stop()


def test_inventory_mutation_request_ids_survive_transport_retries(pair):
    _, inventory = pair
    receipt = {"request_id": str(uuid4()), "ingredient_id": "chicken", "quantity_g": 1000,
               "received_on": app.date.today().isoformat(), "expires_on": None,
               "label": "Fictional retry test"}
    status, first, _ = request(inventory, "/api/receipts", "POST", receipt)
    assert status == 201
    status, repeated, _ = request(inventory, "/api/receipts", "POST", receipt)
    assert status == 201 and repeated == first
    assert len(state(inventory)["batches"]) == 1
    batch_id = first["result"]["id"]
    waste = {"request_id": str(uuid4()), "batch_id": batch_id,
             "quantity_g": 100, "reason": "Fictional prep loss"}
    for _ in range(2):
        assert request(inventory, "/api/waste", "POST", waste)[0] == 201
    assert ingredient(inventory, "chicken")["on_hand_g"] == 900
    count = {"request_id": str(uuid4()), "ingredient_id": "chicken", "quantity_g": 850}
    for _ in range(2):
        assert request(inventory, "/api/counts", "POST", count)[0] == 201
    measured = state(inventory)["counts"]
    assert len(measured) == 1
    assert measured[0]["expected_g"] == 900 and measured[0]["variance_g"] == -50
    receipt["quantity_g"] = 999
    assert request(inventory, "/api/receipts", "POST", receipt)[0] == 400
    assert ingredient(inventory, "chicken")["on_hand_g"] == 900


def test_duplicate_listener_cannot_take_the_existing_order_service_port(pair, tmp_path):
    orders, _ = pair
    original = create(orders)
    duplicate = None
    try:
        with pytest.raises(OSError):
            duplicate = app.make_orders_server(
                tmp_path / "second-service", port=orders.server_port,
                inventory_url="http://127.0.0.1:65534")
    finally:
        if duplicate is not None:
            duplicate.server_close()
    assert state(orders)["orders"][0]["id"] == original["id"]
    assert transition(orders, original["id"], "preparing")["status"] == "preparing"
    assert len(state(orders)["orders"]) == 1
