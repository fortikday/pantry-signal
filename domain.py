"""Pantry Signal's independent order ledger and inventory projection.

Quantities use integer grams. Recipes and dates in the demonstration are fictional.
Order writes never open the inventory database; inventory can catch up later.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4


INGREDIENTS = (
    {"id": "lettuce", "name": "Lettuce", "unit": "g"},
    {"id": "tomato", "name": "Tomatoes", "unit": "g"},
    {"id": "dressing", "name": "Dressing", "unit": "g"},
    {"id": "chicken", "name": "Chicken", "unit": "g"},
    {"id": "shrimp", "name": "Shrimp", "unit": "g"},
)
INGREDIENT_IDS = frozenset(item["id"] for item in INGREDIENTS)
STATUSES = frozenset(("queued", "preparing", "ready", "cancelled"))
MAX_GRAMS = 100_000_000
MAX_REQUIREMENT_GRAMS = 2_000_000_000


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _copy(value):
    return json.loads(_json(value))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _object(value, allowed, required=None):
    if not isinstance(value, dict):
        raise ValueError("Send a JSON object.")
    if set(value) - set(allowed):
        raise ValueError("The request contains an unsupported field.")
    if set(required if required is not None else allowed) - set(value):
        raise ValueError("The request is missing a required field.")
    return value


def _integer(value, name, minimum=0, maximum=MAX_GRAMS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a whole number from {minimum} to {maximum}.")
    return value


def _text(value, name, maximum=100):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"Enter {name} using 1–{maximum} characters.")
    value = value.strip()
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} cannot contain control characters.")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value):
        raise ValueError(f"Choose a valid {name}.")
    return value


def _optional_request_id(payload):
    if "request_id" not in payload:
        return None
    value = payload["request_id"]
    if not isinstance(value, str):
        raise ValueError("Use a valid UUID request ID for this stock change.")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("Use a valid UUID request ID for this stock change.") from None


def _ingredient(value):
    if not isinstance(value, str) or value not in INGREDIENT_IDS:
        raise ValueError("Choose an ingredient from the catalog.")
    return value


def _date(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{name} must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Enter a real calendar date for {name}.") from None


def _requirements(value, *, allow_empty=False):
    if not isinstance(value, list) or len(value) > len(INGREDIENTS) or (not value and not allow_empty):
        raise ValueError("Enter at least one unique recipe ingredient.")
    result, seen = [], set()
    for item in value:
        _object(item, ("ingredient_id", "grams"))
        ingredient_id = _ingredient(item["ingredient_id"])
        if ingredient_id in seen:
            raise ValueError("List each recipe ingredient only once.")
        seen.add(ingredient_id)
        result.append({"ingredient_id": ingredient_id,
                       "grams": _integer(item["grams"], "Ingredient grams", 1, MAX_REQUIREMENT_GRAMS)})
    return sorted(result, key=lambda row: row["ingredient_id"])


def _modifiers(value):
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("Enter a list of up to eight recipe additions.")
    result, seen = [], set()
    for item in value:
        _object(item, ("id", "name", "ingredients"))
        modifier_id = _identifier(item["id"], "addition")
        if modifier_id in seen:
            raise ValueError("List each addition only once.")
        seen.add(modifier_id)
        result.append({"id": modifier_id, "name": _text(item["name"], "addition name", 60),
                       "ingredients": _requirements(item["ingredients"])})
    return sorted(result, key=lambda row: row["id"])


def _initial_menu():
    additions = [
        {"id": "chicken", "name": "Chicken", "ingredients": [{"ingredient_id": "chicken", "grams": 400}]},
        {"id": "shrimp", "name": "Shrimp", "ingredients": [{"ingredient_id": "shrimp", "grams": 120}]},
    ]
    return [
        {"id": "caesar", "name": "Caesar salad", "version": 1,
         "ingredients": [{"ingredient_id": "lettuce", "grams": 100},
                         {"ingredient_id": "tomato", "grams": 80},
                         {"ingredient_id": "dressing", "grams": 30}],
         "modifiers": _copy(additions)},
        {"id": "garden", "name": "Garden salad", "version": 1,
         "ingredients": [{"ingredient_id": "lettuce", "grams": 150},
                         {"ingredient_id": "tomato", "grams": 150},
                         {"ingredient_id": "dressing", "grams": 25}],
         "modifiers": _copy(additions)},
        {"id": "chicken_salad", "name": "Chicken salad", "version": 1,
         "ingredients": [{"ingredient_id": "lettuce", "grams": 100},
                         {"ingredient_id": "tomato", "grams": 80},
                         {"ingredient_id": "dressing", "grams": 30},
                         {"ingredient_id": "chicken", "grams": 400}],
         "modifiers": [_copy(additions[1])]},
    ]


class _Store:
    def __init__(self, path, today=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._today = today or date.today

    def today(self):
        value = self._today()
        if type(value) is date:
            return value
        return _date(value, "Today")

    @contextmanager
    def _connection(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class OrderStore(_Store):
    """The restaurant's source of truth; no inventory server dependency."""

    def __init__(self, path, today=None):
        super().__init__(path, today)
        with self._connection(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS order_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO order_meta(key,value) VALUES ('stream_id',?)", (str(uuid4()),))
            db.execute("CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
                request_body TEXT NOT NULL, body TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                order_id INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
            for item in _initial_menu():
                db.execute("INSERT OR IGNORE INTO recipes(id,body) VALUES (?,?)", (item["id"], _json(item)))

    def state(self):
        with self._connection() as db:
            return {"menu": [json.loads(row["body"]) for row in db.execute("SELECT body FROM recipes ORDER BY id")],
                    "orders": [json.loads(row["body"]) for row in db.execute("SELECT body FROM orders ORDER BY id DESC LIMIT 500")],
                    "stream_id": db.execute("SELECT value FROM order_meta WHERE key='stream_id'").fetchone()[0],
                    "latest_event_id": db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]}

    def source_info(self):
        with self._connection() as db:
            return {"stream_id": db.execute("SELECT value FROM order_meta WHERE key='stream_id'").fetchone()[0],
                    "latest_event_id": db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]}

    def _request(self, payload):
        _object(payload, ("request_id", "table_number", "lines"))
        request_id = payload["request_id"]
        if not isinstance(request_id, str):
            raise ValueError("The order needs a unique request ID.")
        try:
            request_id = str(UUID(request_id))
        except (ValueError, AttributeError):
            raise ValueError("The order needs a valid UUID request ID.") from None
        table_number = _integer(payload["table_number"], "Table number", 1, 100)
        if not isinstance(payload["lines"], list) or not 1 <= len(payload["lines"]) <= 20:
            raise ValueError("Add between one and twenty order lines.")
        lines = []
        for line in payload["lines"]:
            _object(line, ("menu_item_id", "quantity", "modifiers"))
            modifiers = line["modifiers"]
            if not isinstance(modifiers, list) or len(modifiers) > 8:
                raise ValueError("Choose valid additions for this dish.")
            modifiers = [_identifier(value, "addition") for value in modifiers]
            if len(set(modifiers)) != len(modifiers):
                raise ValueError("Choose an addition only once per order line.")
            lines.append({"menu_item_id": _identifier(line["menu_item_id"], "menu item"),
                          "quantity": _integer(line["quantity"], "Portions", 1, 20),
                          "modifiers": sorted(modifiers)})
        return {"request_id": request_id, "table_number": table_number, "lines": lines}

    def _event(self, db, order, kind, previous):
        payload = {"requirements": order["requirements"], "status": order["status"],
                   "previous_status": previous, "business_date": self.today().isoformat()}
        db.execute("INSERT INTO events(type,order_id,payload,created_at) VALUES (?,?,?,?)",
                   (kind, order["id"], _json(payload), _now()))

    def create_order(self, payload):
        request = self._request(payload)
        canonical = _json(request)
        with self._connection(write=True) as db:
            existing = db.execute("SELECT request_body,body FROM orders WHERE request_id=?", (request["request_id"],)).fetchone()
            if existing:
                if existing["request_body"] != canonical:
                    raise ValueError("This request ID already belongs to a different order. Refresh before trying again.")
                return json.loads(existing["body"])
            lines, totals = [], {}
            for requested in request["lines"]:
                row = db.execute("SELECT body FROM recipes WHERE id=?", (requested["menu_item_id"],)).fetchone()
                if row is None:
                    raise ValueError("This menu item is unavailable.")
                recipe = json.loads(row["body"])
                choices = {item["id"]: item for item in recipe["modifiers"]}
                if any(value not in choices for value in requested["modifiers"]):
                    raise ValueError("An addition is unavailable for this dish.")
                ingredients = _copy(recipe["ingredients"])
                selected = [choices[value] for value in requested["modifiers"]]
                for addition in selected:
                    ingredients.extend(_copy(addition["ingredients"]))
                for item in ingredients:
                    totals[item["ingredient_id"]] = totals.get(item["ingredient_id"], 0) + item["grams"] * requested["quantity"]
                    if totals[item["ingredient_id"]] > MAX_REQUIREMENT_GRAMS:
                        raise ValueError("The order's ingredient quantity is too large.")
                lines.append({"menu_item_id": recipe["id"], "name": recipe["name"],
                              "quantity": requested["quantity"], "modifiers": [item["name"] for item in selected],
                              "recipe_version": recipe["version"],
                              "recipe_snapshot": {"ingredients": _copy(recipe["ingredients"]), "modifiers": _copy(selected)}})
            order = {"request_id": request["request_id"], "table_number": request["table_number"],
                     "status": "queued", "created_at": _now(), "lines": lines,
                     "requirements": [{"ingredient_id": key, "grams": value} for key, value in sorted(totals.items())]}
            cursor = db.execute("INSERT INTO orders(request_id,request_body,body) VALUES (?,?,?)", (request["request_id"], canonical, "{}"))
            order["id"] = cursor.lastrowid
            db.execute("UPDATE orders SET body=? WHERE id=?", (_json(order), order["id"]))
            self._event(db, order, "order.created", None)
            return order

    def transition_order(self, order_id, status):
        _integer(order_id, "Order ID", 1, 2_000_000_000)
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Choose a valid kitchen status.")
        with self._connection(write=True) as db:
            row = db.execute("SELECT body FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise ValueError("This order does not exist.")
            order = json.loads(row["body"])
            previous = order["status"]
            if previous == status:
                return order
            allowed = {"queued": {"preparing", "cancelled"}, "preparing": {"ready", "cancelled"},
                       "ready": {"cancelled"}, "cancelled": set()}
            if status not in allowed[previous]:
                raise ValueError("This order cannot move to that kitchen status.")
            order["status"] = status
            order["updated_at"] = _now()
            db.execute("UPDATE orders SET body=? WHERE id=?", (_json(order), order_id))
            self._event(db, order, "order." + status, previous)
            return order

    def events(self, after=0, limit=100):
        _integer(after, "Event cursor", 0, 2_000_000_000)
        _integer(limit, "Event page size", 1, 1000)
        with self._connection() as db:
            return [{"id": row["id"], "type": row["type"], "order_id": row["order_id"],
                     "payload": json.loads(row["payload"]), "created_at": row["created_at"]}
                    for row in db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))]

    def save_recipe(self, payload):
        _object(payload, ("menu_item_id", "version", "ingredients", "modifiers"))
        menu_id = _identifier(payload["menu_item_id"], "menu item")
        version = _integer(payload["version"], "Recipe version", 1, 2_000_000_000)
        ingredients = _requirements(payload["ingredients"])
        modifiers = _modifiers(payload["modifiers"])
        with self._connection(write=True) as db:
            row = db.execute("SELECT body FROM recipes WHERE id=?", (menu_id,)).fetchone()
            if row is None:
                raise ValueError("This menu item does not exist.")
            recipe = json.loads(row["body"])
            if recipe["version"] != version:
                raise ValueError("This recipe changed elsewhere. Reload before saving.")
            if _requirements(recipe["ingredients"]) == ingredients and _modifiers(recipe["modifiers"]) == modifiers:
                return recipe
            recipe.update(ingredients=ingredients, modifiers=modifiers, version=version + 1)
            db.execute("UPDATE recipes SET body=? WHERE id=?", (_json(recipe), menu_id))
            return recipe


class InventoryStore(_Store):
    """An independently replayable projection with batch allocation assumptions.

    Stock mutation payloads should include a stable UUID ``request_id``. It makes
    transport retries return the original result without repeating the change.
    Legacy local callers may omit it, but then have no retry deduplication.
    """

    def __init__(self, path, today=None):
        super().__init__(path, today)
        with self._connection(write=True) as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)",
                """CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ingredient_id TEXT NOT NULL,quantity_g INTEGER NOT NULL,remaining_g INTEGER NOT NULL,
                    received_on TEXT NOT NULL,expires_on TEXT,label TEXT NOT NULL,created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS waste (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id),ingredient_id TEXT NOT NULL,
                    quantity_g INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS counts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ingredient_id TEXT NOT NULL,quantity_g INTEGER NOT NULL,expected_g INTEGER NOT NULL,
                    variance_g INTEGER NOT NULL,created_at TEXT NOT NULL)""",
                "CREATE TABLE IF NOT EXISTS processed_events (id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL)",
                """CREATE TABLE IF NOT EXISTS mutation_requests (request_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,request_body TEXT NOT NULL,result_body TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS projection_orders (order_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,requirements TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS usage (order_id INTEGER NOT NULL,ingredient_id TEXT NOT NULL,
                    grams INTEGER NOT NULL,allocated_g INTEGER NOT NULL,PRIMARY KEY(order_id,ingredient_id))""",
                """CREATE TABLE IF NOT EXISTS allocations (order_id INTEGER NOT NULL,batch_id INTEGER NOT NULL
                    REFERENCES batches(id),grams INTEGER NOT NULL,PRIMARY KEY(order_id,batch_id))""",
            ):
                db.execute(statement)
            for key, value in (("last_event_id", "0"), ("paused", "false"), ("error", "null"),
                               ("last_sync", "null"), ("demo_seeded", "false"), ("stream_id", "null")):
                db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (key, value))

    @staticmethod
    def _meta(db):
        return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key,value FROM meta")}

    @staticmethod
    def _put(db, key, value):
        db.execute("UPDATE meta SET value=? WHERE key=?", (_json(value), key))

    @staticmethod
    def _balance(db, ingredient_id):
        received = db.execute("SELECT COALESCE(SUM(quantity_g),0) FROM batches WHERE ingredient_id=?", (ingredient_id,)).fetchone()[0]
        used = db.execute("SELECT COALESCE(SUM(grams),0) FROM usage WHERE ingredient_id=?", (ingredient_id,)).fetchone()[0]
        wasted = db.execute("SELECT COALESCE(SUM(quantity_g),0) FROM waste WHERE ingredient_id=?", (ingredient_id,)).fetchone()[0]
        return received - used - wasted

    @staticmethod
    def _cached_mutation(db, kind, request_id, request_body):
        if request_id is None:
            return None
        existing = db.execute("SELECT kind,request_body,result_body FROM mutation_requests WHERE request_id=?",
                              (request_id,)).fetchone()
        if existing is None:
            return None
        if existing["kind"] != kind or existing["request_body"] != _json(request_body):
            raise ValueError("This request ID already belongs to a different stock change. Start a new entry before trying again.")
        return json.loads(existing["result_body"])

    @staticmethod
    def _save_mutation(db, kind, request_id, request_body, result):
        if request_id is not None:
            db.execute("INSERT INTO mutation_requests(request_id,kind,request_body,result_body) VALUES (?,?,?,?)",
                       (request_id, kind, _json(request_body), _json(result)))
        return result

    def state(self):
        today = self.today()
        with self._connection() as db:
            meta = self._meta(db)
            reserved = {item["id"]: 0 for item in INGREDIENTS}
            for row in db.execute("SELECT requirements FROM projection_orders WHERE status='queued'"):
                for item in json.loads(row["requirements"]):
                    reserved[item["ingredient_id"]] += item["grams"]
            batches = []
            for row in db.execute("SELECT * FROM batches ORDER BY expires_on IS NULL,expires_on,id"):
                item = dict(row)
                expires = date.fromisoformat(item["expires_on"]) if item["expires_on"] else None
                status = ("unknown" if expires is None else "expired" if expires < today else
                          "use_soon" if expires <= today + timedelta(days=2) else "dated")
                item.update(expiry_status=status, eligible=expires is not None and expires >= today)
                batches.append(item)
            ingredients = []
            for ingredient in INGREDIENTS:
                ingredient_id = ingredient["id"]
                relevant = [batch for batch in batches if batch["ingredient_id"] == ingredient_id]
                eligible = sum(batch["remaining_g"] for batch in relevant if batch["eligible"])
                unallocated = db.execute("SELECT COALESCE(SUM(grams-allocated_g),0) FROM usage WHERE ingredient_id=?", (ingredient_id,)).fetchone()[0]
                on_hand = self._balance(db, ingredient_id)
                ingredients.append({**ingredient, "on_hand_g": on_hand,
                                    "reserved_g": reserved[ingredient_id], "available_g": min(eligible, on_hand) - reserved[ingredient_id],
                                    "eligible_g": eligible, "unallocated_g": unallocated,
                                    "expired_g": sum(batch["remaining_g"] for batch in relevant if batch["expiry_status"] == "expired"),
                                    "unknown_expiry_g": sum(batch["remaining_g"] for batch in relevant if batch["expiry_status"] == "unknown")})
            return {"today": today.isoformat(), "ingredients": ingredients, "batches": batches,
                    "counts": [dict(row) for row in db.execute("SELECT * FROM counts ORDER BY id DESC LIMIT 100")],
                    "waste": [dict(row) for row in db.execute("SELECT * FROM waste ORDER BY id DESC LIMIT 100")],
                    "sync": {key: meta[key] for key in ("last_event_id", "paused", "error", "last_sync", "stream_id")},
                    "demo_seeded": meta["demo_seeded"]}

    def receive(self, payload):
        fields = ("ingredient_id", "quantity_g", "received_on", "expires_on", "label")
        _object(payload, fields + ("request_id",), required=fields)
        request_id = _optional_request_id(payload)
        ingredient_id = _ingredient(payload["ingredient_id"])
        quantity = _integer(payload["quantity_g"], "Received grams", 1)
        received = _date(payload["received_on"], "Received date")
        expires = None if payload["expires_on"] is None else _date(payload["expires_on"], "Expiry date")
        if expires is not None and expires < received:
            raise ValueError("The expiry date cannot be before the received date.")
        label = _text(payload["label"], "batch label")
        canonical = {"ingredient_id": ingredient_id, "quantity_g": quantity, "received_on": received.isoformat(),
                     "expires_on": expires.isoformat() if expires else None, "label": label}
        with self._connection(write=True) as db:
            cached = self._cached_mutation(db, "receive", request_id, canonical)
            if cached is not None:
                return cached
            if received > self.today():
                raise ValueError("The received date cannot be in the future.")
            cursor = db.execute("""INSERT INTO batches(ingredient_id,quantity_g,remaining_g,received_on,expires_on,label,created_at)
                VALUES (?,?,?,?,?,?,?)""", (ingredient_id, quantity, quantity, received.isoformat(),
                                            expires.isoformat() if expires else None, label, _now()))
            result = dict(db.execute("SELECT * FROM batches WHERE id=?", (cursor.lastrowid,)).fetchone())
            return self._save_mutation(db, "receive", request_id, canonical, result)

    def waste(self, payload):
        fields = ("batch_id", "quantity_g", "reason")
        _object(payload, fields + ("request_id",), required=fields)
        request_id = _optional_request_id(payload)
        batch_id = _integer(payload["batch_id"], "Batch ID", 1, 2_000_000_000)
        quantity = _integer(payload["quantity_g"], "Waste grams", 1)
        reason = _text(payload["reason"], "waste reason", 200)
        canonical = {"batch_id": batch_id, "quantity_g": quantity, "reason": reason}
        with self._connection(write=True) as db:
            cached = self._cached_mutation(db, "waste", request_id, canonical)
            if cached is not None:
                return cached
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("Choose a received batch.")
            if quantity > batch["remaining_g"]:
                raise ValueError("Waste cannot exceed the batch's recorded remaining quantity.")
            cursor = db.execute("INSERT INTO waste(batch_id,ingredient_id,quantity_g,reason,created_at) VALUES (?,?,?,?,?)",
                                (batch_id, batch["ingredient_id"], quantity, reason, _now()))
            db.execute("UPDATE batches SET remaining_g=remaining_g-? WHERE id=?", (quantity, batch_id))
            result = dict(db.execute("SELECT * FROM waste WHERE id=?", (cursor.lastrowid,)).fetchone())
            return self._save_mutation(db, "waste", request_id, canonical, result)

    def count(self, payload):
        fields = ("ingredient_id", "quantity_g")
        _object(payload, fields + ("request_id",), required=fields)
        request_id = _optional_request_id(payload)
        ingredient_id = _ingredient(payload["ingredient_id"])
        quantity = _integer(payload["quantity_g"], "Counted grams")
        canonical = {"ingredient_id": ingredient_id, "quantity_g": quantity}
        with self._connection(write=True) as db:
            cached = self._cached_mutation(db, "count", request_id, canonical)
            if cached is not None:
                return cached
            expected = self._balance(db, ingredient_id)
            cursor = db.execute("INSERT INTO counts(ingredient_id,quantity_g,expected_g,variance_g,created_at) VALUES (?,?,?,?,?)",
                                (ingredient_id, quantity, expected, quantity - expected, _now()))
            result = dict(db.execute("SELECT * FROM counts WHERE id=?", (cursor.lastrowid,)).fetchone())
            return self._save_mutation(db, "count", request_id, canonical, result)

    def seed_demo(self):
        today = self.today()
        with self._connection(write=True) as db:
            if any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("batches", "projection_orders", "counts", "processed_events", "waste")) or self._meta(db)["demo_seeded"]:
                raise ValueError("Example deliveries can only be added to a completely empty inventory log.")
            for ingredient, quantity, days in (("lettuce", 5000, 2), ("tomato", 5000, 4),
                                               ("dressing", 3000, 3), ("chicken", 5000, 2), ("shrimp", 2000, 1)):
                db.execute("""INSERT INTO batches(ingredient_id,quantity_g,remaining_g,received_on,expires_on,label,created_at)
                    VALUES (?,?,?,?,?,?,?)""", (ingredient, quantity, quantity, today.isoformat(),
                                                (today + timedelta(days=days)).isoformat(), "Fictional demo delivery", _now()))
            self._put(db, "demo_seeded", True)
        return {"seeded": True, "batches_added": 5}

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise ValueError("The paused setting must be true or false.")
        with self._connection(write=True) as db:
            self._put(db, "paused", paused)
        return {"paused": paused}

    def set_sync_error(self, error):
        if error is not None:
            error = _text(error, "sync message", 500)
        with self._connection(write=True) as db:
            self._put(db, "error", error)

    @staticmethod
    def _validated_event(event):
        _object(event, ("id", "type", "order_id", "payload", "created_at"))
        event_id = _integer(event["id"], "Event ID", 1, 2_000_000_000)
        order_id = _integer(event["order_id"], "Order ID", 1, 2_000_000_000)
        kind = event["type"]
        if not isinstance(kind, str) or kind not in {"order.created", "order.preparing", "order.ready", "order.cancelled"}:
            raise ValueError("The source contains an unsupported order event.")
        payload = event["payload"]
        _object(payload, ("requirements", "status", "previous_status", "business_date"))
        requirements = _requirements(payload["requirements"])
        business_date = _date(payload["business_date"], "Event business date").isoformat()
        if not isinstance(payload["status"], str) or payload["status"] not in STATUSES or (
            payload["previous_status"] is not None and (not isinstance(payload["previous_status"], str) or payload["previous_status"] not in STATUSES)
        ):
            raise ValueError("The source event has invalid kitchen statuses.")
        created_at = _text(event["created_at"], "event date", 50)
        try:
            timestamp = datetime.fromisoformat(created_at)
            if timestamp.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError("The source event needs a timezone-aware timestamp.") from None
        normalized = {"id": event_id, "order_id": order_id, "type": kind,
                      "payload": {"requirements": requirements, "status": payload["status"],
                                  "previous_status": payload["previous_status"], "business_date": business_date},
                      "created_at": created_at}
        return normalized, hashlib.sha256(_json(normalized).encode("utf-8")).hexdigest()

    def _project(self, db, event):
        order_id, payload = event["order_id"], event["payload"]
        requirements = payload["requirements"]
        row = db.execute("SELECT * FROM projection_orders WHERE order_id=?", (order_id,)).fetchone()
        if event["type"] == "order.created":
            if row is not None or payload["previous_status"] is not None or payload["status"] != "queued":
                raise ValueError("The source event cannot create this inventory reservation.")
            db.execute("INSERT INTO projection_orders(order_id,status,requirements) VALUES (?,?,?)",
                       (order_id, "queued", _json(requirements)))
            return
        if row is None or row["status"] != payload["previous_status"] or json.loads(row["requirements"]) != requirements:
            raise ValueError("The source event does not match the previous order snapshot.")
        target = {"order.preparing": "preparing", "order.ready": "ready", "order.cancelled": "cancelled"}[event["type"]]
        allowed = {"queued": {"preparing", "cancelled"}, "preparing": {"ready", "cancelled"},
                   "ready": {"cancelled"}, "cancelled": set()}
        if payload["status"] != target or target not in allowed[row["status"]]:
            raise ValueError("The source event has an invalid kitchen transition.")
        if target == "preparing":
            business_date = payload["business_date"]
            for item in requirements:
                remaining = item["grams"]
                for batch in db.execute("""SELECT id,remaining_g FROM batches WHERE ingredient_id=?
                    AND received_on<=? AND expires_on>=? AND remaining_g>0 ORDER BY expires_on,received_on,id""",
                                        (item["ingredient_id"], business_date, business_date)).fetchall():
                    allocated = min(remaining, batch["remaining_g"])
                    if allocated:
                        db.execute("UPDATE batches SET remaining_g=remaining_g-? WHERE id=?", (allocated, batch["id"]))
                        db.execute("INSERT INTO allocations(order_id,batch_id,grams) VALUES (?,?,?)", (order_id, batch["id"], allocated))
                        remaining -= allocated
                    if remaining == 0:
                        break
                db.execute("INSERT INTO usage(order_id,ingredient_id,grams,allocated_g) VALUES (?,?,?,?)",
                           (order_id, item["ingredient_id"], item["grams"], item["grams"] - remaining))
        db.execute("UPDATE projection_orders SET status=?,consumed=? WHERE order_id=?",
                   (target, 1 if target == "preparing" or row["consumed"] else 0, order_id))

    def apply_events(self, events, *, stream_id=None, latest_event_id=None):
        if not isinstance(events, list) or len(events) > 1000:
            raise ValueError("Send an event page with up to 1000 events.")
        if stream_id is not None:
            if not isinstance(stream_id, str):
                raise ValueError("The order source needs a valid stream ID.")
            try:
                stream_id = str(UUID(stream_id))
            except ValueError:
                raise ValueError("The order source needs a valid stream ID.") from None
        if latest_event_id is not None:
            _integer(latest_event_id, "Latest source event ID", 0, 2_000_000_000)
        normalized = [self._validated_event(event) for event in events]
        with self._connection(write=True) as db:
            meta = self._meta(db)
            if meta["stream_id"] is not None and stream_id != meta["stream_id"]:
                raise ValueError("The orders database has changed identity. Keep this inventory log and investigate before syncing another source.")
            if latest_event_id is not None and latest_event_id < meta["last_event_id"]:
                raise ValueError("The orders database event history moved backwards. Keep both logs and investigate before resuming.")
            if latest_event_id is not None and any(event["id"] > latest_event_id for event, _ in normalized):
                raise ValueError("The source event page is newer than its reported event cursor. Fetch a fresh page.")
            if stream_id is not None and meta["stream_id"] is None:
                self._put(db, "stream_id", stream_id)
            if meta["paused"]:
                return {"applied": 0, "last_event_id": meta["last_event_id"], "paused": True}
            last_id, applied = meta["last_event_id"], 0
            for event, fingerprint in normalized:
                existing = db.execute("SELECT fingerprint FROM processed_events WHERE id=?", (event["id"],)).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise ValueError("A previously processed event changed. Stop sync and investigate the source.")
                    continue
                if event["id"] != last_id + 1:
                    raise ValueError("An order event is missing. Resume from the saved event cursor.")
                self._project(db, event)
                db.execute("INSERT INTO processed_events(id,fingerprint) VALUES (?,?)", (event["id"], fingerprint))
                last_id, applied = event["id"], applied + 1
            self._put(db, "last_event_id", last_id)
            self._put(db, "last_sync", _now())
            self._put(db, "error", None)
            return {"applied": applied, "last_event_id": last_id, "paused": False}
