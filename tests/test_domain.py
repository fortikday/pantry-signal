"""Business invariants across the restaurant's two independent databases."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from domain import INGREDIENTS, InventoryStore, OrderStore

TODAY = date(2026, 10, 22)


@pytest.fixture
def stores(tmp_path):
    return (OrderStore(tmp_path / "orders.sqlite3", today=lambda: TODAY),
            InventoryStore(tmp_path / "inventory.sqlite3", today=lambda: TODAY))


def request(*, quantity=1, modifiers=None, table=7):
    return {"request_id": str(uuid4()), "table_number": table,
            "lines": [{"menu_item_id": "caesar", "quantity": quantity,
                       "modifiers": ["chicken", "shrimp"] if modifiers is None else modifiers}]}


def ingredient(inventory, name):
    return next(row for row in inventory.state()["ingredients"] if row["id"] == name)


def receive(inventory, *, name="chicken", quantity=1000, expiry="2026-10-24", received="2026-10-22", label="Test batch"):
    return inventory.receive({"ingredient_id": name, "quantity_g": quantity,
                              "received_on": received, "expires_on": expiry, "label": label})


def sync(orders, inventory):
    cursor = inventory.state()["sync"]["last_event_id"]
    return inventory.apply_events(orders.events(cursor), **orders.source_info())


def test_empty_inventory_is_never_seeded_automatically(stores):
    orders, inventory = stores
    assert len(orders.state()["menu"]) == 3
    assert orders.state()["orders"] == []
    assert inventory.state()["batches"] == []
    assert inventory.state()["demo_seeded"] is False
    assert all(row["on_hand_g"] == row["reserved_g"] == row["available_g"] == 0
               for row in inventory.state()["ingredients"])


def test_table_seven_recipe_snapshot_reserves_then_consumes_once(stores):
    orders, inventory = stores
    inventory.seed_demo()
    order = orders.create_order(request())
    assert order["table_number"] == 7
    assert order["status"] == "queued"
    assert {row["ingredient_id"]: row["grams"] for row in order["requirements"]} == {
        "chicken": 400, "shrimp": 120, "lettuce": 100, "tomato": 80, "dressing": 30}
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert sync(orders, inventory)["applied"] == 1
    assert ingredient(inventory, "chicken")["on_hand_g"] == 5000
    assert ingredient(inventory, "chicken")["reserved_g"] == 400
    assert ingredient(inventory, "chicken")["available_g"] == 4600
    orders.transition_order(order["id"], "preparing")
    sync(orders, inventory)
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4600
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert ingredient(inventory, "shrimp")["on_hand_g"] == 1880
    orders.transition_order(order["id"], "ready")
    sync(orders, inventory)
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4600
    assert inventory.state()["sync"]["last_event_id"] == 3
    assert inventory.apply_events(orders.events(0), **orders.source_info())["applied"] == 0
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4600


def test_restaurant_and_kitchen_work_before_inventory_catches_up(stores):
    orders, inventory = stores
    order = orders.create_order(request(quantity=2))
    orders.transition_order(order["id"], "preparing")
    orders.transition_order(order["id"], "ready")
    assert orders.state()["orders"][0]["status"] == "ready"
    assert inventory.state()["sync"]["last_event_id"] == 0
    inventory.seed_demo()
    assert sync(orders, inventory)["applied"] == 3
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4200


def test_cancel_before_preparation_releases_reservation_without_consumption(stores):
    orders, inventory = stores
    inventory.seed_demo()
    order = orders.create_order(request())
    sync(orders, inventory)
    orders.transition_order(order["id"], "cancelled")
    sync(orders, inventory)
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert ingredient(inventory, "chicken")["on_hand_g"] == 5000
    assert all(batch["remaining_g"] == batch["quantity_g"] for batch in inventory.state()["batches"])


@pytest.mark.parametrize("ready_first", [False, True])
def test_cancel_after_cooking_keeps_usage_recorded(stores, ready_first):
    orders, inventory = stores
    inventory.seed_demo()
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    if ready_first:
        orders.transition_order(order["id"], "ready")
    orders.transition_order(order["id"], "cancelled")
    sync(orders, inventory)
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4600
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert inventory.state()["waste"] == []  # Cancellation is not proof of disposal.


def test_same_request_retry_returns_original_order_even_after_recipe_change(stores):
    orders, inventory = stores
    payload = request()
    first = orders.create_order(payload)
    recipe = next(item for item in orders.state()["menu"] if item["id"] == "caesar")
    recipe["modifiers"][0]["ingredients"][0]["grams"] = 200
    saved = orders.save_recipe({"menu_item_id": recipe["id"], "version": recipe["version"],
                                 "ingredients": recipe["ingredients"], "modifiers": recipe["modifiers"]})
    assert saved["version"] == 2
    assert orders.create_order(payload) == first
    assert len(orders.state()["orders"]) == 1
    assert len(orders.events()) == 1
    second = orders.create_order(request())
    assert {row["ingredient_id"]: row["grams"] for row in second["requirements"]}["chicken"] == 200
    assert {row["ingredient_id"]: row["grams"] for row in first["requirements"]}["chicken"] == 400
    assert first["lines"][0]["recipe_version"] == 1


def test_changed_request_payload_cannot_reuse_an_order_id(stores):
    orders, _ = stores
    payload = request()
    orders.create_order(payload)
    payload["table_number"] = 8
    with pytest.raises(ValueError, match="different order"):
        orders.create_order(payload)
    assert len(orders.events()) == 1


def test_identical_modifiers_in_different_order_are_the_same_request(stores):
    orders, _ = stores
    payload = request()
    first = orders.create_order(payload)
    payload["lines"][0]["modifiers"].reverse()
    assert orders.create_order(payload) == first


def test_kitchen_status_retry_does_not_emit_an_extra_event(stores):
    orders, _ = stores
    order = orders.create_order(request())
    first = orders.transition_order(order["id"], "preparing")
    assert orders.transition_order(order["id"], "preparing") == first
    assert len(orders.events()) == 2


@pytest.mark.parametrize("status", ["ready", "invalid", "", [], None])
def test_invalid_kitchen_status_or_skipped_preparation_does_not_write(stores, status):
    orders, _ = stores
    order = orders.create_order(request())
    with pytest.raises(ValueError):
        orders.transition_order(order["id"], status)
    assert orders.state()["orders"][0]["status"] == "queued"
    assert len(orders.events()) == 1


def test_cancelled_order_cannot_be_reopened(stores):
    orders, _ = stores
    order = orders.create_order(request())
    orders.transition_order(order["id"], "cancelled")
    with pytest.raises(ValueError):
        orders.transition_order(order["id"], "preparing")


def test_multiple_order_lines_and_portions_aggregate_ingredients(stores):
    orders, _ = stores
    payload = request(quantity=2)
    payload["lines"].append({"menu_item_id": "garden", "quantity": 3, "modifiers": []})
    order = orders.create_order(payload)
    expected = {row["ingredient_id"]: row["grams"] for row in order["requirements"]}
    assert expected["chicken"] == 800
    assert expected["lettuce"] == 650
    assert expected["tomato"] == 610


@pytest.mark.parametrize("quantity", [True, False, 1.0, "1", 0, -1, 21, None])
def test_portions_require_bounded_whole_numbers(stores, quantity):
    orders, _ = stores
    with pytest.raises(ValueError):
        orders.create_order(request(quantity=quantity))
    assert orders.state()["orders"] == []
    assert orders.events() == []


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(table_number=True),
    lambda p: p.update(table_number=0),
    lambda p: p.update(table_number=101),
    lambda p: p.update(request_id="bad"),
    lambda p: p.update(lines=[]),
    lambda p: p["lines"][0].update(menu_item_id="missing"),
    lambda p: p["lines"][0].update(modifiers=["missing"]),
    lambda p: p["lines"][0].update(modifiers=["chicken", "chicken"]),
    lambda p: p.update(unrecognized=True),
])
def test_invalid_order_rolls_back_the_whole_request(stores, mutation):
    orders, _ = stores
    payload = request()
    mutation(payload)
    with pytest.raises(ValueError):
        orders.create_order(payload)
    assert orders.state()["orders"] == []
    assert orders.events() == []


def test_unknown_second_line_does_not_leave_a_partial_order(stores):
    orders, _ = stores
    payload = request()
    payload["lines"].append({"menu_item_id": "missing", "quantity": 1, "modifiers": []})
    with pytest.raises(ValueError):
        orders.create_order(payload)
    assert orders.events() == []
    assert orders.state()["orders"] == []


def test_fefo_allocates_earliest_dated_batch_and_excludes_expired_unknown(stores):
    orders, inventory = stores
    expired = receive(inventory, name="lettuce", quantity=100, received="2026-10-20", expiry="2026-10-21", label="Expired")
    unknown = receive(inventory, name="lettuce", quantity=100, expiry=None, label="Unknown date")
    later = receive(inventory, name="lettuce", quantity=200, expiry="2026-10-25", label="Later")
    first = receive(inventory, name="lettuce", quantity=100, expiry="2026-10-23", label="Earlier")
    payload = request(modifiers=[])
    payload["lines"][0]["menu_item_id"] = "garden"
    order = orders.create_order(payload)
    orders.transition_order(order["id"], "preparing")
    sync(orders, inventory)
    batches = {row["id"]: row for row in inventory.state()["batches"]}
    assert batches[first["id"]]["remaining_g"] == 0
    assert batches[later["id"]]["remaining_g"] == 150
    assert batches[expired["id"]]["remaining_g"] == 100
    assert batches[unknown["id"]]["remaining_g"] == 100
    assert ingredient(inventory, "lettuce")["on_hand_g"] == 350
    assert ingredient(inventory, "lettuce")["eligible_g"] == 150
    assert ingredient(inventory, "lettuce")["available_g"] == 150


def test_expiry_today_is_included_but_not_a_food_safety_assertion(stores):
    orders, inventory = stores
    batch = receive(inventory, expiry="2026-10-22")
    assert inventory.state()["batches"][0]["expiry_status"] == "use_soon"
    order = orders.create_order(request(modifiers=["chicken"]))
    orders.transition_order(order["id"], "preparing")
    sync(orders, inventory)
    assert inventory.state()["batches"][0]["remaining_g"] == 600
    assert ingredient(inventory, "chicken")["unallocated_g"] == 0


def test_delayed_replay_uses_preparation_date_and_excludes_later_deliveries(tmp_path):
    day = [date(2026, 10, 22)]
    orders = OrderStore(tmp_path / "dated-orders.sqlite3", today=lambda: day[0])
    inventory = InventoryStore(tmp_path / "dated-inventory.sqlite3", today=lambda: day[0])
    old = receive(inventory, quantity=100, expiry="2026-10-22", label="Available on cooking day")
    order = orders.create_order(request(modifiers=["chicken"]))
    orders.transition_order(order["id"], "preparing")
    assert orders.events()[1]["payload"]["business_date"] == "2026-10-22"
    day[0] = date(2026, 10, 23)
    later = receive(inventory, quantity=500, received="2026-10-23", expiry="2026-10-26", label="Arrived after cooking")
    sync(orders, inventory)
    batches = {batch["id"]: batch for batch in inventory.state()["batches"]}
    assert batches[old["id"]]["remaining_g"] == 0
    assert batches[later["id"]]["remaining_g"] == 500
    assert ingredient(inventory, "chicken")["unallocated_g"] == 300
    assert ingredient(inventory, "chicken")["on_hand_g"] == 200
    assert ingredient(inventory, "chicken")["available_g"] == 200


def test_shortage_is_recorded_without_stalling_events_and_later_receipt_is_conservative(stores):
    orders, inventory = stores
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    orders.transition_order(order["id"], "ready")
    assert sync(orders, inventory)["applied"] == 3
    chicken = ingredient(inventory, "chicken")
    assert chicken["on_hand_g"] == chicken["available_g"] == -400
    assert chicken["unallocated_g"] == 400
    receive(inventory, quantity=500)
    chicken = ingredient(inventory, "chicken")
    assert chicken["on_hand_g"] == chicken["available_g"] == 100
    assert chicken["eligible_g"] == 500
    assert chicken["unallocated_g"] == 400


def test_stock_count_records_a_difference_without_rewriting_estimates(stores):
    orders, inventory = stores
    inventory.seed_demo()
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    sync(orders, inventory)
    count = inventory.count({"ingredient_id": "chicken", "quantity_g": 4500})
    assert (count["expected_g"], count["variance_g"]) == (4600, -100)
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4600
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    sync(orders, inventory)
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4200
    assert inventory.state()["counts"][0]["expected_g"] == 4600


def test_waste_uses_a_specific_batch_and_decreases_the_ledger(stores):
    _, inventory = stores
    batch = receive(inventory)
    waste = inventory.waste({"batch_id": batch["id"], "quantity_g": 200, "reason": "Preparation trim"})
    assert waste["ingredient_id"] == "chicken"
    assert ingredient(inventory, "chicken")["on_hand_g"] == 800
    assert inventory.state()["batches"][0]["remaining_g"] == 800
    with pytest.raises(ValueError):
        inventory.waste({"batch_id": batch["id"], "quantity_g": 801, "reason": "Too much"})
    assert inventory.state()["batches"][0]["remaining_g"] == 800


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(quantity_g=True),
    lambda p: p.update(quantity_g=0),
    lambda p: p.update(quantity_g=1.1),
    lambda p: p.update(quantity_g="100"),
    lambda p: p.update(ingredient_id="unknown"),
    lambda p: p.update(received_on="2026-10-23"),
    lambda p: p.update(received_on="2026-2-01"),
    lambda p: p.update(received_on="2026-02-30"),
    lambda p: p.update(expires_on="2026-10-21"),
    lambda p: p.update(expires_on="invalid"),
    lambda p: p.update(label=""),
])
def test_invalid_receipt_is_not_saved(stores, mutation):
    _, inventory = stores
    payload = {"ingredient_id": "chicken", "quantity_g": 1000,
               "received_on": "2026-10-22", "expires_on": "2026-10-24", "label": "Delivery"}
    mutation(payload)
    with pytest.raises(ValueError):
        inventory.receive(payload)
    assert inventory.state()["batches"] == []


def test_demo_seed_is_explicit_fictional_and_one_time(stores):
    _, inventory = stores
    assert inventory.seed_demo()["batches_added"] == 5
    state = inventory.state()
    assert state["demo_seeded"] is True
    assert len(state["batches"]) == 5
    assert all("Fictional demo" in batch["label"] for batch in state["batches"])
    assert any(batch["expires_on"] == "2026-10-23" for batch in state["batches"])
    with pytest.raises(ValueError):
        inventory.seed_demo()
    assert len(inventory.state()["batches"]) == 5


@pytest.mark.parametrize("existing", ["receipt", "count", "event"])
def test_example_deliveries_cannot_be_mixed_into_existing_inventory(stores, existing):
    orders, inventory = stores
    if existing == "receipt":
        receive(inventory)
    elif existing == "count":
        inventory.count({"ingredient_id": "lettuce", "quantity_g": 0})
    else:
        orders.create_order(request())
        sync(orders, inventory)
    with pytest.raises(ValueError):
        inventory.seed_demo()


def test_pause_prevents_an_in_flight_fetch_from_advancing_projection(stores):
    orders, inventory = stores
    inventory.seed_demo()
    orders.create_order(request())
    fetched = orders.events()
    inventory.set_paused(True)
    result = inventory.apply_events(fetched, **orders.source_info())
    assert result["paused"] is True
    assert inventory.state()["sync"]["last_event_id"] == 0
    assert inventory.state()["sync"]["stream_id"] == orders.source_info()["stream_id"]
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    inventory.set_paused(False)
    sync(orders, inventory)
    assert ingredient(inventory, "chicken")["reserved_g"] == 400


def test_event_page_with_a_gap_is_atomic_and_preserves_cursor(stores):
    orders, inventory = stores
    orders.create_order(request())
    order = orders.state()["orders"][0]
    orders.transition_order(order["id"], "preparing")
    events = orders.events()
    events[1]["id"] = 3
    with pytest.raises(ValueError, match="missing"):
        inventory.apply_events(events)
    assert inventory.state()["sync"]["last_event_id"] == 0
    assert ingredient(inventory, "chicken")["reserved_g"] == 0
    assert ingredient(inventory, "chicken")["on_hand_g"] == 0


def test_changed_duplicate_event_is_rejected_without_modifying_inventory(stores):
    orders, inventory = stores
    orders.create_order(request())
    sync(orders, inventory)
    events = orders.events()
    events[0]["payload"]["requirements"][0]["grams"] += 1
    before = inventory.state()
    with pytest.raises(ValueError, match="previously processed event changed"):
        inventory.apply_events(events, **orders.source_info())
    assert inventory.state() == before


def test_transition_cannot_change_the_original_ingredient_snapshot(stores):
    orders, inventory = stores
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    events = orders.events()
    events[1]["payload"]["requirements"][0]["grams"] += 1
    with pytest.raises(ValueError, match="snapshot"):
        inventory.apply_events(events)
    assert inventory.state()["sync"]["last_event_id"] == 0


@pytest.mark.parametrize("status", [[], {}, 1, False, "invalid"])
def test_malformed_event_status_fails_cleanly(stores, status):
    orders, inventory = stores
    orders.create_order(request())
    event = orders.events()[0]
    event["payload"]["status"] = status
    with pytest.raises(ValueError):
        inventory.apply_events([event])


def test_empty_first_sync_binds_source_and_rejects_another_database(stores, tmp_path):
    orders, inventory = stores
    sync(orders, inventory)
    other = OrderStore(tmp_path / "other-orders.sqlite3", today=lambda: TODAY)
    assert orders.source_info()["stream_id"] != other.source_info()["stream_id"]
    before = inventory.state()
    with pytest.raises(ValueError, match="changed identity"):
        inventory.apply_events([], **other.source_info())
    assert inventory.state() == before


def test_inventory_rejects_a_source_cursor_rewind(stores):
    orders, inventory = stores
    orders.create_order(request())
    sync(orders, inventory)
    info = orders.source_info()
    info["latest_event_id"] = 0
    before = inventory.state()
    with pytest.raises(ValueError, match="backwards"):
        inventory.apply_events([], **info)
    assert inventory.state() == before


def test_bound_inventory_requires_source_identity_on_later_calls(stores):
    orders, inventory = stores
    sync(orders, inventory)
    with pytest.raises(ValueError, match="changed identity"):
        inventory.apply_events([])


def test_source_identity_and_projection_survive_reopening_files(stores):
    orders, inventory = stores
    inventory.seed_demo()
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    sync(orders, inventory)
    reopened_orders = OrderStore(orders.path, today=lambda: TODAY)
    reopened_inventory = InventoryStore(inventory.path, today=lambda: TODAY)
    assert reopened_orders.source_info() == orders.source_info()
    assert reopened_inventory.state() == inventory.state()
    assert sync(reopened_orders, reopened_inventory)["applied"] == 0
    assert ingredient(reopened_inventory, "chicken")["on_hand_g"] == 4600


def test_recipe_version_conflict_and_noop_save(stores):
    orders, _ = stores
    recipe = next(item for item in orders.state()["menu"] if item["id"] == "caesar")
    payload = {"menu_item_id": recipe["id"], "version": recipe["version"],
               "ingredients": recipe["ingredients"], "modifiers": recipe["modifiers"]}
    assert orders.save_recipe(payload)["version"] == 1
    payload["ingredients"][0]["grams"] += 10
    assert orders.save_recipe(payload)["version"] == 2
    with pytest.raises(ValueError, match="changed elsewhere"):
        orders.save_recipe(payload)


def test_recipe_duplicate_ingredient_and_unknown_addition_are_rejected(stores):
    orders, _ = stores
    recipe = next(item for item in orders.state()["menu"] if item["id"] == "caesar")
    payload = {"menu_item_id": recipe["id"], "version": recipe["version"],
               "ingredients": recipe["ingredients"] + recipe["ingredients"][:1], "modifiers": recipe["modifiers"]}
    with pytest.raises(ValueError, match="only once"):
        orders.save_recipe(payload)
    assert next(item for item in orders.state()["menu"] if item["id"] == "caesar")["version"] == 1


def test_concurrent_order_retries_create_only_one_order_and_event(stores):
    orders, _ = stores
    payload = request()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: orders.create_order(deepcopy(payload)), range(16)))
    assert {item["id"] for item in results} == {1}
    assert len(orders.state()["orders"]) == len(orders.events()) == 1


def test_concurrent_duplicate_projection_does_not_double_deduct(stores):
    orders, inventory = stores
    inventory.seed_demo()
    order = orders.create_order(request())
    orders.transition_order(order["id"], "preparing")
    events, info = orders.events(), orders.source_info()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: inventory.apply_events(deepcopy(events), **info), range(16)))
    assert sum(item["applied"] for item in results) == 2
    assert ingredient(inventory, "chicken")["on_hand_g"] == 4600


def mutation_payload(inventory, kind, request_id):
    if kind == "receive":
        return {"request_id": request_id, "ingredient_id": "chicken", "quantity_g": 1000,
                "received_on": "2026-10-22", "expires_on": "2026-10-24", "label": "Delivery"}
    batch = receive(inventory)
    if kind == "waste":
        return {"request_id": request_id, "batch_id": batch["id"], "quantity_g": 600, "reason": "Recorded trim"}
    return {"request_id": request_id, "ingredient_id": "chicken", "quantity_g": 900}


@pytest.mark.parametrize("kind", ["receive", "waste", "count"])
def test_stock_mutation_retry_after_response_loss_returns_original_result(stores, kind):
    _, inventory = stores
    payload = mutation_payload(inventory, kind, str(uuid4()))
    first = getattr(inventory, kind)(payload)
    # The first request committed, but its caller lost the response. Retry from
    # a new process/store, after current stock can have changed in the meantime.
    if kind == "count":
        receive(inventory, quantity=500)
    reopened = InventoryStore(inventory.path, today=lambda: TODAY)
    assert getattr(reopened, kind)(deepcopy(payload)) == first
    state = reopened.state()
    if kind == "receive":
        assert len(state["batches"]) == 1
        assert ingredient(reopened, "chicken")["on_hand_g"] == 1000
    elif kind == "waste":
        assert len(state["waste"]) == 1
        assert ingredient(reopened, "chicken")["on_hand_g"] == 400
    else:
        assert len(state["counts"]) == 1
        assert first["expected_g"] == 1000
        assert ingredient(reopened, "chicken")["on_hand_g"] == 1500


@pytest.mark.parametrize("kind", ["receive", "waste", "count"])
def test_stock_request_id_rejects_changed_business_payload(stores, kind):
    _, inventory = stores
    payload = mutation_payload(inventory, kind, str(uuid4()))
    getattr(inventory, kind)(payload)
    before = inventory.state()
    payload["quantity_g"] += 1
    with pytest.raises(ValueError, match="different stock change"):
        getattr(inventory, kind)(payload)
    assert inventory.state() == before


@pytest.mark.parametrize("kind", ["waste", "count"])
def test_stock_request_id_cannot_be_reused_for_another_mutation_kind(stores, kind):
    _, inventory = stores
    request_id = str(uuid4())
    receive_payload = mutation_payload(inventory, "receive", request_id)
    batch = inventory.receive(receive_payload)
    if kind == "waste":
        payload = {"request_id": request_id, "batch_id": batch["id"], "quantity_g": 100, "reason": "Trim"}
    else:
        payload = {"request_id": request_id, "ingredient_id": "chicken", "quantity_g": 900}
    before = inventory.state()
    with pytest.raises(ValueError, match="different stock change"):
        getattr(inventory, kind)(payload)
    assert inventory.state() == before


@pytest.mark.parametrize("request_id", [None, True, 123, "bad", {}, []])
def test_provided_stock_request_id_must_be_a_uuid(stores, request_id):
    _, inventory = stores
    payload = mutation_payload(inventory, "receive", request_id)
    with pytest.raises(ValueError, match="UUID request ID"):
        inventory.receive(payload)
    assert inventory.state()["batches"] == []


def test_inventory_snapshot_date_and_expiry_roll_over_together_at_midnight(tmp_path):
    day = [date(2026, 10, 22)]
    clock_calls = []
    def clock():
        clock_calls.append(day[0])
        return day[0]
    inventory = InventoryStore(tmp_path / "midnight-inventory.sqlite3", today=clock)
    receive(inventory, quantity=1000, expiry="2026-10-22")
    clock_calls.clear()

    before = inventory.state()
    assert before["today"] == "2026-10-22"
    assert before["batches"][0]["eligible"] is True
    assert before["batches"][0]["expiry_status"] == "use_soon"
    chicken = next(row for row in before["ingredients"] if row["id"] == "chicken")
    assert chicken["available_g"] == 1000
    assert clock_calls == [date(2026, 10, 22)]

    day[0] = date(2026, 10, 23)
    after = inventory.state()
    assert after["today"] == "2026-10-23"
    assert after["batches"][0]["eligible"] is False
    assert after["batches"][0]["expiry_status"] == "expired"
    chicken = next(row for row in after["ingredients"] if row["id"] == "chicken")
    assert chicken["available_g"] == 0
    assert chicken["on_hand_g"] == chicken["expired_g"] == 1000
    assert clock_calls == [date(2026, 10, 22), date(2026, 10, 23)]


def test_concurrent_delivery_retries_create_one_batch(stores):
    _, inventory = stores
    payload = mutation_payload(inventory, "receive", str(uuid4()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: inventory.receive(deepcopy(payload)), range(16)))
    assert len(inventory.state()["batches"]) == 1
    assert all(item == results[0] for item in results)
    assert ingredient(inventory, "chicken")["on_hand_g"] == 1000
