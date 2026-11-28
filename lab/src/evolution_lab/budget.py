"""Persistent upper-tariff reservations for the Lab gateway."""

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from uuid import uuid4

import httpx

from evolution_lab.durability import Durability, effective


class BudgetExhausted(RuntimeError):
    pass


def _amount(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2**62:
        raise ValueError("invalid budget amount")
    return value


class BudgetLedger:
    def __init__(self, path: Path, *, limit_units: int, identity: str, durability: Durability | None = None):
        self._durability = effective() if durability is None else durability
        self._anchor = None
        _amount(limit_units)
        if not identity or path.is_symlink():
            raise ValueError("invalid budget identity/path")
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # SQLite derives WAL/SHM permissions from the main file at first open.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.open_anchor()
        try:
            self._initialize(limit_units, identity)
        except BaseException:
            self.close_anchor()
            raise

    def _initialize(self, limit_units, identity):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS configuration (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL, limit_units INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, role TEXT NOT NULL, model TEXT NOT NULL, request_hash TEXT NOT NULL, reserved INTEGER NOT NULL, settled INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            if "settled_source" not in columns:
                # Legacy settlements keep NULL and are reported as tariff amounts.
                db.execute("ALTER TABLE requests ADD COLUMN settled_source TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS usage_cost_missing (request_id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS routing_observations (request_id TEXT PRIMARY KEY, requested_provider TEXT, reported_provider TEXT, reported_model TEXT, requested_providers TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(routing_observations)")}
            if "requested_providers" not in columns:
                # Ledgers created before ordered provider sets keep their rows.
                db.execute("ALTER TABLE routing_observations ADD COLUMN requested_providers TEXT")
            for column in ("endpoint", "client_model", "upstream_model", "uses_json"):
                if column not in columns:
                    db.execute(f"ALTER TABLE routing_observations ADD COLUMN {column} TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS refusals (id TEXT PRIMARY KEY, requested INTEGER NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS balance_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), floor_rub REAL NOT NULL, authorization TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS balance_samples (balance_rub REAL NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS overruns (request_id TEXT PRIMARY KEY, reason TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS bound_observations (request_id TEXT PRIMARY KEY, reason TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS request_failures (request_id TEXT PRIMARY KEY, status INTEGER, code TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS released_reservations (request_id TEXT PRIMARY KEY, status INTEGER, reserved INTEGER NOT NULL, authorization TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS release_authorizations (authorization TEXT NOT NULL, released_refusals INTEGER NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS resume_authorizations (authorization TEXT NOT NULL, cleared_refusals INTEGER NOT NULL, cleared_overruns INTEGER NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            row = db.execute(
                "SELECT identity,limit_units FROM configuration WHERE singleton=1"
            ).fetchone()
            if row is None:
                db.execute("INSERT INTO configuration VALUES (1,?,?)", (identity, limit_units))
            elif row != (identity, limit_units):
                raise ValueError("budget configuration differs from checkpoint")
            db.commit()

    def activate_balance_floor(self, floor_rub, *, authorization):
        if (
            isinstance(floor_rub, bool)
            or not isinstance(floor_rub, (int, float))
            or not math.isfinite(floor_rub)
            or floor_rub <= 0
            or not authorization
        ):
            raise ValueError("explicit positive balance floor and authorization required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM balance_policy").fetchone()[0]:
                raise ValueError("balance policy already set")
            db.execute(
                "CREATE TABLE IF NOT EXISTS archived_refusals AS SELECT * FROM refusals WHERE 0"
            )
            db.execute("INSERT INTO archived_refusals SELECT * FROM refusals")
            db.execute("DELETE FROM refusals")
            db.execute(
                "INSERT INTO balance_policy(singleton,floor_rub,authorization) VALUES(1,?,?)",
                (floor_rub, authorization),
            )
            db.commit()

    def revise_balance_floor(self, floor_rub, *, authorization):
        """Replace an active account floor while preserving an audit trail."""
        if (
            isinstance(floor_rub, bool)
            or not isinstance(floor_rub, (int, float))
            or not math.isfinite(floor_rub)
            or floor_rub <= 0
            or not authorization
        ):
            raise ValueError("explicit positive balance floor and authorization required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT floor_rub,authorization FROM balance_policy WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise ValueError("balance policy is not set")
            db.execute(
                "CREATE TABLE IF NOT EXISTS balance_policy_history ("
                "old_floor REAL NOT NULL,new_floor REAL NOT NULL,"
                "old_authorization TEXT NOT NULL,new_authorization TEXT NOT NULL,"
                "created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "INSERT INTO balance_policy_history"
                "(old_floor,new_floor,old_authorization,new_authorization) VALUES(?,?,?,?)",
                (current[0], floor_rub, current[1], authorization),
            )
            db.execute(
                "UPDATE balance_policy SET floor_rub=?,authorization=?,created=CURRENT_TIMESTAMP "
                "WHERE singleton=1",
                (floor_rub, authorization),
            )
            db.commit()

    def authorize_resume(self, *, authorization):
        """Lift an accounting stop by explicit operator authorization, keeping the trail.

        Clears refusals and overruns after archiving them; bound observations are
        history and stay. A stop whose cause still holds is recorded again on the
        next request.
        """
        if not isinstance(authorization, str) or not authorization.strip():
            raise ValueError("explicit resume authorization required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cleared = [
                db.execute("SELECT COUNT(*) FROM refusals").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM overruns").fetchone()[0],
            ]
            for table in ("refusals", "overruns"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS archived_{table} AS SELECT * FROM {table} WHERE 0"
                )
                db.execute(f"INSERT INTO archived_{table} SELECT * FROM {table}")
                db.execute(f"DELETE FROM {table}")
            db.execute(
                "INSERT INTO resume_authorizations(authorization,cleared_refusals,cleared_overruns)"
                " VALUES(?,?,?)",
                (authorization, *cleared),
            )
            db.commit()

    def resume_authorizations(self):
        with self.connection() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT authorization FROM resume_authorizations ORDER BY rowid"
                )
            ]

    def balance_floor(self):
        with self.connection() as db:
            row = db.execute("SELECT floor_rub FROM balance_policy WHERE singleton=1").fetchone()
            return row[0] if row else None

    def record_failure(self, request_id, status, code):
        """Record the upstream status (None for transport) and code, never message text.

        This records evidence only; explicit upstream refusals are released separately.
        """
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO request_failures (request_id, status, code) VALUES (?,?,?)",
                       (request_id, status, code))
            db.commit()

    @staticmethod
    def _release_reservation(db, request_id, status, reserved, authorization):
        db.execute(
            "INSERT INTO released_reservations(request_id,status,reserved,authorization) VALUES(?,?,?,?)",
            (request_id, status, reserved, authorization),
        )
        db.execute("UPDATE requests SET settled=0,settled_source='refused' WHERE id=?", (request_id,))

    def release_refused(self, request_id, status):
        """Release an explicit upstream refusal and retain its reservation audit."""
        if (not isinstance(status, int) or isinstance(status, bool)
                or not (200 <= status <= 299 or 400 <= status <= 499)):
            raise ValueError("upstream 2xx or 4xx status required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reserved,settled,settled_source FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown reservation")
            if row[1] is None:
                self._release_reservation(db, request_id, status, row[0], None)
            elif row[1:] != (0, "refused"):
                raise ValueError("settled usage cannot change")
            db.commit()

    def release_refused_reservations(self, *, authorization: str) -> int:
        """Release historical upstream 4xx reservations without lifting a budget stop."""
        if not isinstance(authorization, str) or not authorization.strip():
            raise ValueError("explicit release authorization required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT r.id,f.status,r.reserved FROM requests r "
                "JOIN request_failures f ON f.request_id=r.id "
                "WHERE r.settled IS NULL AND f.status BETWEEN 400 AND 499"
            ).fetchall()
            for request_id, status, reserved in rows:
                self._release_reservation(db, request_id, status, reserved, authorization)
            db.execute(
                "INSERT INTO release_authorizations(authorization,released_refusals) VALUES(?,?)",
                (authorization, len(rows)),
            )
            db.commit()
            return len(rows)

    def record_routing(self, request_id, requested, payload=None):
        """Record a pinned provider name or a whole ordered provider set."""
        payload = payload or {}
        reported = payload.get("provider")
        model = payload.get("model")
        provider_set = (
            json.dumps(list(requested), separators=(",", ":"))
            if isinstance(requested, tuple) else None
        )
        with self.connection() as db:
            db.execute(
                "INSERT INTO routing_observations "
                "(request_id, requested_provider, reported_provider, reported_model, requested_providers) "
                "VALUES (?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "requested_provider=excluded.requested_provider, reported_provider=excluded.reported_provider, "
                "reported_model=excluded.reported_model, requested_providers=excluded.requested_providers",
                (
                    request_id,
                    requested if isinstance(requested, str) else None,
                    reported if isinstance(reported, str) else None,
                    model if isinstance(model, str) else None,
                    provider_set,
                ),
            )
            db.commit()

    def record_usage_cost_missing(self, request_id):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO usage_cost_missing VALUES (?)", (request_id,))
            db.commit()

    def record_balance(self, balance, *, apply_global_floor=True):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            floor = db.execute("SELECT floor_rub FROM balance_policy WHERE singleton=1").fetchone()
            db.execute("INSERT INTO balance_samples(balance_rub) VALUES(?)", (balance,))
            if (
                apply_global_floor and floor
                and balance <= floor[0]
                and not db.execute("SELECT COUNT(*) FROM refusals").fetchone()[0]
            ):
                db.execute("INSERT INTO refusals(id,requested) VALUES(?,0)", (str(uuid4()),))
            db.commit()

    def open_anchor(self):
        """Hold the database open so per-operation closes do not checkpoint WAL."""
        if self._anchor is not None:
            return
        db = sqlite3.connect(self.path, timeout=30)
        try:
            journal = "wal" if self._durability == 'relaxed' else "delete"
            if db.execute(f"PRAGMA journal_mode={journal}").fetchone()[0] != journal:
                raise sqlite3.OperationalError("could not set journal mode")
            db.execute("PRAGMA synchronous=NORMAL" if self._durability == 'relaxed' else "PRAGMA synchronous=FULL")
            # Reading the schema attaches the WAL; a merely configured idle connection
            # does not prevent other connections from doing last-close checkpoints.
            db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except BaseException:
            db.close()
            raise
        self._anchor = db

    def close_anchor(self):
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=NORMAL" if self._durability == 'relaxed' else "PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _used(db):
        return db.execute(
            "SELECT COALESCE(SUM(COALESCE(settled,reserved)),0) FROM requests"
        ).fetchone()[0]

    def reserve(self, units: int, *, role: str, model: str, request_hash: str, routing=None):
        _amount(units)
        if not all(isinstance(v, str) and v for v in (role, model, request_hash)):
            raise ValueError("missing request identity")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            limit = db.execute(
                "SELECT limit_units FROM configuration WHERE singleton=1"
            ).fetchone()[0]
            if (db.execute("SELECT COUNT(*) FROM refusals").fetchone()[0]
                    or db.execute("SELECT COUNT(*) FROM overruns").fetchone()[0]):
                raise BudgetExhausted("campaign budget is stopped")
            # Lab's explicit experiment cap applies even when an account floor
            # is enabled. An account balance is not spending authorization.
            if self._used(db) + units > limit:
                db.execute(
                    "INSERT INTO refusals (id,requested) VALUES (?,?)", (str(uuid4()), units)
                )
                db.commit()
                raise BudgetExhausted("insufficient budget reservation")
            request_id = str(uuid4())
            db.execute(
                "INSERT INTO requests (id,role,model,request_hash,reserved) VALUES (?,?,?,?,?)",
                (request_id, role, model, request_hash, units),
            )
            if routing is not None:
                db.execute("INSERT INTO routing_observations (request_id,endpoint,client_model,upstream_model,uses_json) VALUES (?,?,?,?,?)", (request_id, *routing))
            db.commit()
            return request_id

    def settle(
        self, request_id: str, actual_upper_units: int | None, *, source: str,
        tariff_actual: int | None, bounds_exceeded: bool = False,
    ):
        if actual_upper_units is None:
            return  # Unknown billing keeps the entire pre-call reservation.
        _amount(actual_upper_units)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reserved,settled FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown reservation")
            if row[1] is not None and row[1] != actual_upper_units:
                raise ValueError("settled usage cannot change")
            tariff_bound = tariff_actual if tariff_actual is not None else 0
            if bounds_exceeded or row[0] < actual_upper_units <= tariff_bound:
                # Extra reported usage can exceed the request-sized reservation
                # without exceeding the route tariff for those tokens.
                db.execute(
                    "INSERT OR IGNORE INTO bound_observations VALUES (?,?)",
                    (request_id, "provider_usage_exceeds_bound"),
                )
            if actual_upper_units > max(row[0], tariff_bound):
                db.execute("INSERT OR IGNORE INTO overruns VALUES (?,?)", (request_id, "settled_exceeds_reservation"))
            db.execute("UPDATE requests SET settled=?, settled_source=? WHERE id=?",
                       (actual_upper_units, source, request_id))
            db.commit()

    def summary(self):
        with self.connection() as db:
            db.execute("BEGIN")
            limit = db.execute(
                "SELECT limit_units FROM configuration WHERE singleton=1"
            ).fetchone()[0]
            used = self._used(db)
            requests = db.execute("SELECT COUNT(*),COUNT(settled) FROM requests").fetchone()
            settled_upstream, settled_tariff = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN settled_source='upstream_cost' THEN settled ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN settled_source='tariff' OR settled_source IS NULL THEN settled ELSE 0 END),0) "
                "FROM requests"
            ).fetchone()
            failures = db.execute(
                "SELECT COALESCE(CAST(status AS TEXT),'transport'), COUNT(*) FROM request_failures GROUP BY 1"
            ).fetchall()
            by_status = {"transport": 0, **{key: count for key, count in failures}}
            return {
                "limit_units": limit,
                "reserved_upper_units": used,
                "available_units": limit - used,
                "requests": requests[0],
                "unknown_requests": requests[0] - requests[1],
                "settled_upstream_units": settled_upstream,
                "settled_tariff_units": settled_tariff,
                "usage_cost_missing": db.execute("SELECT COUNT(*) FROM usage_cost_missing").fetchone()[0],
                "released_refusals": db.execute("SELECT COUNT(*) FROM released_reservations").fetchone()[0],
                "overrun_requests": db.execute("SELECT COUNT(*) FROM overruns").fetchone()[0],
                "bound_observations": db.execute("SELECT COUNT(*) FROM bound_observations").fetchone()[0],
                "refusals": db.execute("SELECT COUNT(*) FROM refusals").fetchone()[0],
                "failed_requests": sum(by_status.values()),
                "failed_by_status": by_status,
            }


class Price:
    """Upper tariff assumptions, in currency units per million tokens."""

    def __init__(self, input_per_million, output_per_million, context_tokens):
        self.input = Decimal(str(input_per_million))
        self.output = Decimal(str(output_per_million))
        if not all(value.is_finite() and value >= 0 for value in (self.input, self.output)):
            raise ValueError("invalid tariff")
        if _amount(context_tokens) < 1:
            raise ValueError("invalid context bound")
        self.context_tokens = context_tokens

    def units(self, incoming, outgoing):
        _amount(incoming)
        _amount(outgoing)
        # One accounting unit is 1e-9 of the configured currency.
        return int(
            ((self.input * incoming + self.output * outgoing) * 1000).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    def tariff_units(self, usage) -> int | None:
        if not isinstance(usage, Mapping):
            return None
        incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (incoming, outgoing)
        ):
            return None
        return self.units(incoming, outgoing)


def _route_protocol(base_url: str) -> str | None:
    normalized = base_url.rstrip("/")
    if normalized == "https://openrouter.ai/api/v1":
        return "openrouter"
    if normalized == "https://routerai.ru/api/v1":
        return "routerai"
    url = httpx.URL(normalized)
    if url.host in {"localhost", "127.0.0.1"}:
        if url.path.rstrip("/").endswith("/openrouter/api/v1"):
            return "openrouter"
        if url.path.rstrip("/").endswith("/routerai/api/v1"):
            return "routerai"
    return None


_FAILURE_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _failure_code(payload):
    """A short code from an upstream error body, or None: only a token, never the message."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        for key in ("code", "type"):
            value = error.get(key)
            if isinstance(value, str) and _FAILURE_CODE.fullmatch(value):
                return value
    return None


class _RawRefusal(dict):
    """Keep the decoded payload API while preserving upstream bytes for the HTTP adapter."""

    def __init__(self, payload, content):
        super().__init__(payload)
        self.content = content


@dataclass(frozen=True)
class Route:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    price: Price
    max_tokens: int
    timeout_seconds: float = 300
    provider: str | None = None
    providers: tuple[str, ...] | None = None
    reasoning_effort: str | None = None
    client_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    client_model: str | None = field(default=None, kw_only=True)
    keyless: bool = field(default=False, kw_only=True)
    allow_fallbacks: bool = field(default=False, kw_only=True)
    pass_reasoning: bool = field(default=False, kw_only=True)
    balance_floor: float | None = field(default=None, kw_only=True)
    provider_only: tuple[str, ...] | None = field(default=None, kw_only=True)
    policy_floor: bool = field(default=False, kw_only=True, repr=False)
    policy_order: bool = field(default=False, kw_only=True, repr=False)

    def __post_init__(self):
        if self.client_model is not None and (not isinstance(self.client_model, str) or not self.client_model):
            raise ValueError("invalid client model")
        if self.allow_fallbacks and self.providers is None:
            raise ValueError("fallbacks require ordered providers")
        if self.provider_only is not None and (self.provider is not None or self.providers is not None):
            raise ValueError("provider pins are mutually exclusive")
        if self.balance_floor is not None and (isinstance(self.balance_floor, bool) or not isinstance(self.balance_floor, (int, float)) or not math.isfinite(self.balance_floor) or self.balance_floor <= 0):
            raise ValueError("invalid balance floor")
        if not isinstance(self.client_token, str) or not self.client_token or self.client_token == self.api_key:
            raise ValueError("distinct nonempty gateway client token required")
        if self.reasoning_effort is not None and self.reasoning_effort not in ("low", "medium", "high", "max", "minimal", "none"):
            raise ValueError("invalid reasoning effort")
        url = httpx.URL(self.base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query:
            raise ValueError("invalid upstream endpoint")
        names = None
        if self.provider is not None:
            names = (self.provider,)
            if self.providers is not None:
                raise ValueError("provider and providers are mutually exclusive")
        elif self.providers is not None or self.provider_only is not None:
            names = self.providers if self.providers is not None else self.provider_only
            if not isinstance(names, tuple) or not names or len(set(names)) != len(names):
                raise ValueError("invalid ordered provider set")
        if names is not None and (
            not all(isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9/._-]*", name) for name in names)
            or _route_protocol(self.base_url) not in {"routerai", "openrouter"}
            or "@" in self.model
        ):
            raise ValueError("invalid pinned provider route")
        if (
            (not self.api_key and not (self.keyless and self.api_key == ""))
            or not self.model
            or _amount(self.max_tokens) < 1
            or self.timeout_seconds <= 0
        ):
            raise ValueError("invalid upstream bounds")


def settle_amount(route: Route, currency: str, usage: Mapping) -> tuple[int, str] | None:
    tariff_actual = route.price.tariff_units(usage)
    if tariff_actual is None:
        return None
    expected_currency = {"openrouter": "USD", "routerai": "RUB"}.get(_route_protocol(route.base_url))
    cost = usage.get("cost")
    if expected_currency == currency and isinstance(cost, (int, float)) and not isinstance(cost, bool):
        amount = Decimal(str(cost))
        if amount.is_finite() and amount >= 0:
            return int((amount * 10**9).to_integral_value(rounding=ROUND_CEILING)), "upstream_cost"
    return tariff_actual, "tariff"


class Gateway:
    def __init__(
        self, ledger: BudgetLedger, routes: dict[str, Route], *, transport=None, currency="RUB",
        role_groups: dict[str, tuple[str, ...]] | None = None,
    ):
        if currency not in {"RUB", "USD"}:
            raise ValueError("unsupported accounting currency")
        self.currency = currency
        self._uses = frozenset()
        self.ledger, self.routes, self.transport = ledger, dict(routes), transport
        if any(route.client_token == other.api_key for route in self.routes.values() for other in self.routes.values()):
            raise ValueError("client tokens must differ from every upstream key")
        self.role_groups = {key: tuple(value) for key, value in (role_groups or {}).items()}
        for group, members in self.role_groups.items():
            if (
                not group or group in self.routes or not members
                or len(set(members)) != len(members)
                or any(member not in self.routes for member in members)
                or len({(self.routes[member].client_model or self.routes[member].model) for member in members}) != len(members)
            ):
                raise ValueError("invalid or ambiguous role group")

    def set_uses(self, uses: frozenset[str]) -> None:
        if not isinstance(uses, frozenset) or any(not isinstance(use, str) for use in uses):
            raise ValueError("uses must be a frozenset of strings")
        self._uses = uses

    def exhausted(self):
        summary = self.ledger.summary()
        return summary["refusals"] > 0 or summary["overrun_requests"] > 0

    async def complete(self, role, body, authorization):
        def error(status, code):
            return status, {"error": {"code": code, "message": code}}

        if not isinstance(body, dict):
            return error(400, "invalid_request")
        if role in self.role_groups:
            candidates = [
                name
                for name in self.role_groups[role]
                if (self.routes[name].client_model or self.routes[name].model) == body.get("model")
            ]
            if len(candidates) != 1:
                return error(400, "invalid_group_model")
            role = candidates[0]
        route = self.routes.get(role)
        if route is None or body.get("model") != (route.client_model or route.model):
            return error(400, "invalid_role_or_model")
        if not isinstance(authorization, str) or not hmac.compare_digest(
            authorization, "Bearer " + route.client_token
        ):
            return error(401, "invalid_gateway_credentials")
        allowed = {
            "model",
            "messages",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "functions",
            "function_call",
            "response_format",
            "seed",
            "user",
            "stream",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
        }
        if route.pass_reasoning:
            allowed.add("reasoning")
        if set(body) - allowed:
            return error(400, "unpriced_request_features")
        body = {**body, "model": route.model}
        if route.provider_only:
            body = {**body, "provider": {"only": list(route.provider_only), "allow_fallbacks": False}}
        elif route.provider:
            body = {**body, "provider": {"only": [route.provider], "allow_fallbacks": False}}
        elif route.providers:
            body = {**body, "provider": {
                **({} if route.policy_order or route.allow_fallbacks else {"only": list(route.providers)}),
                "order": list(route.providers), "allow_fallbacks": route.allow_fallbacks,
            }}
        if route.reasoning_effort is not None:
            body = {**body, "reasoning": {"effort": route.reasoning_effort}}
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            return error(400, "invalid_messages")
        for message in messages:
            if not isinstance(message, dict):
                return error(400, "invalid_messages")
            content = message.get("content")
            if isinstance(content, list):
                if any(
                    not isinstance(part, dict)
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                    for part in content
                ):
                    return error(400, "unpriced_message_content")
            elif content is not None and not isinstance(content, str):
                return error(400, "invalid_messages")
        if "max_tokens" in body and "max_completion_tokens" in body:
            return error(400, "ambiguous_output_bounds")
        if "max_completion_tokens" in body:
            body = dict(body)
            body["max_tokens"] = body.pop("max_completion_tokens")
        maximum = body.get("max_tokens")
        if (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or not 1 <= maximum <= route.max_tokens
            or body.get("stream")
        ):
            return error(400, "invalid_request_bounds")
        try:
            encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError):
            return error(400, "invalid_request")
        if self.exhausted():
            return error(429, "budget_exhausted")
        floor = route.balance_floor if route.policy_floor or route.balance_floor is not None else self.ledger.balance_floor()
        if floor is not None:
            expected_currency = {"routerai": "RUB", "openrouter": "USD"}.get(
                _route_protocol(route.base_url)
            )
            if expected_currency != self.currency:
                return error(503, "unsupported_balance_provider")
            try:
                async with httpx.AsyncClient(
                    timeout=10, trust_env=False, transport=self.transport
                ) as client:
                    response = await client.get(
                        route.base_url.rstrip("/")
                        + ("/key" if self.currency == "USD" else "/credits"),
                        headers={"Authorization": "Bearer " + route.api_key},
                    )
                response.raise_for_status()
                data = response.json()["data"]
                if self.currency == "USD":
                    balance = data["limit_remaining"]
                else:
                    balance = data["credits"]
                if (
                    isinstance(balance, bool)
                    or not isinstance(balance, (int, float))
                    or not math.isfinite(balance)
                ):
                    raise ValueError("invalid balance")
                self.ledger.record_balance(balance, apply_global_floor=not route.policy_floor and route.balance_floor is None)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return error(503, "account_balance_unavailable")
            if balance <= floor:
                return error(402, "account_balance_floor_reached")
        try:
            reservation = self.ledger.reserve(
                route.price.units(min(route.price.context_tokens, len(encoded)), maximum),
                role=role,
                model=route.model,
                request_hash=hashlib.sha256(encoded).hexdigest(),
                routing=(route.base_url, route.client_model or route.model, route.model, json.dumps(sorted(self._uses), separators=(",", ":"))),
            )
        except BudgetExhausted:
            return error(429, "budget_exhausted")
        self.ledger.record_routing(reservation, route.provider or route.providers or route.provider_only)
        # Unknown billing keeps its reservation; an explicit upstream refusal releases it (ADR-0025).
        try:
            async with httpx.AsyncClient(
                timeout=route.timeout_seconds, trust_env=False, transport=self.transport
            ) as client:
                response = await client.post(
                    route.base_url.rstrip("/") + "/chat/completions",
                    headers={} if route.keyless else {"Authorization": "Bearer " + route.api_key},
                    json=body,
                )
        except httpx.HTTPError:
            self.ledger.record_failure(reservation, None, "upstream_unavailable")
            return error(502, "upstream_unavailable_or_invalid")
        try:
            payload = response.json()
            json_ok = True
        except ValueError:
            payload = None
            json_ok = False
        if response.status_code >= 400:
            self.ledger.record_failure(reservation, response.status_code, _failure_code(payload) or "http_error")
            if response.status_code <= 499:
                self.ledger.release_refused(reservation, response.status_code)
            if not isinstance(payload, dict):
                return error(502, "invalid_upstream_response" if json_ok else "upstream_unavailable_or_invalid")
            self.ledger.record_routing(reservation, route.provider or route.providers or route.provider_only, payload)
            return response.status_code, payload
        if not isinstance(payload, dict):
            self.ledger.record_failure(reservation, response.status_code, "upstream_invalid")
            return error(502, "invalid_upstream_response" if json_ok else "upstream_unavailable_or_invalid")
        self.ledger.record_routing(reservation, route.provider or route.providers or route.provider_only, payload)
        if 200 <= response.status_code <= 299 and "error" in payload and "choices" not in payload:
            self.ledger.record_failure(reservation, response.status_code, _failure_code(payload) or "http_error")
            self.ledger.release_refused(reservation, response.status_code)
            return response.status_code, _RawRefusal(payload, response.content)
        usage = payload.get("usage")
        settlement = settle_amount(route, self.currency, usage)
        if settlement is None:
            self.ledger.record_failure(reservation, response.status_code, "usage_missing")
            return response.status_code, payload
        units, source = settlement
        if source == "tariff" and _route_protocol(route.base_url) in {"openrouter", "routerai"}:
            self.ledger.record_usage_cost_missing(reservation)
        self.ledger.settle(
            reservation, units, source=source, tariff_actual=route.price.tariff_units(usage),
            bounds_exceeded=(
                usage["prompt_tokens"] > route.price.context_tokens
                or usage["completion_tokens"] > maximum
            )
        )
        return response.status_code, payload


def gateway_from_policy(policy, *, upstream_keys: Mapping[str, str], tokens: Mapping[str, str], transport=None, durability: Durability | None = None) -> Gateway:
    """Construct routes using secret-name and route-name bindings, respectively."""
    expected_keys = {route.api_key_secret for route in policy.routes.values() if route.api_key_secret is not None}
    if set(upstream_keys) != expected_keys or set(tokens) != set(policy.routes):
        raise ValueError("invalid policy bindings")
    if any(not isinstance(value, str) or not value for value in (*upstream_keys.values(), *tokens.values())):
        raise ValueError("empty policy binding")
    routes = {}
    for name, spec in policy.routes.items():
        pin = spec.provider
        routes[name] = Route(spec.endpoint, upstream_keys[spec.api_key_secret] if spec.api_key_secret else "",
                             spec.model, spec.price, spec.max_tokens, spec.timeout_seconds,
                             providers=pin.order if pin else None, client_token=tokens[name],
                             client_model=spec.client_model, keyless=spec.api_key_secret is None,
                             provider_only=pin.only if pin else None,
                             allow_fallbacks=pin.allow_fallbacks if pin else False,
                             pass_reasoning=True, balance_floor=spec.balance_floor, policy_floor=True, policy_order=True)
    block = policy.gateway
    ledger = BudgetLedger(Path(block.ledger_path), limit_units=block.limit_units, identity=block.ledger_identity, durability=durability)
    return Gateway(ledger, routes, currency=block.currency, role_groups=policy.role_groups, transport=transport)

def create_app(gateway: Gateway):
    """Scoped donor HTTP adapter; serve on loopback with access logs disabled.

    Requires the optional `gateway` extra. Server lifecycle belongs to the
    caller. No provider fallback or campaign semantics are added here.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {**gateway.ledger.summary(), "budget_exhausted": gateway.exhausted(),
                "currency": gateway.currency, "balance_floor": gateway.ledger.balance_floor()}

    @app.get("/{role}/v1/models")
    async def models(role: str, request: Request):
        if role in gateway.role_groups:
            routes = [gateway.routes[name] for name in gateway.role_groups[role]]
        elif role in gateway.routes:
            routes = [gateway.routes[role]]
        else:
            return JSONResponse({"error": {"code": "invalid_role_or_model"}}, status_code=404)
        authorization = request.headers.get("authorization")
        if not isinstance(authorization, str) or not authorization.isascii() or not any(
            route.client_token.isascii()
            and hmac.compare_digest(authorization, "Bearer " + route.client_token) for route in routes
        ):
            return JSONResponse({"error": {"code": "invalid_gateway_credentials"}}, status_code=401)
        return {"object": "list", "data": [
            {"id": route.client_model or route.model, "object": "model", "owned_by": "lab-gateway"} for route in routes
        ]}

    @app.post("/{role}/v1/chat/completions")
    async def completion(role: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": {"code": "invalid_json"}}, status_code=400)
        status, payload = await gateway.complete(role, body, request.headers.get("authorization"))
        if isinstance(payload, _RawRefusal):
            return Response(payload.content, status_code=status, media_type="application/json")
        return JSONResponse(payload, status_code=status)

    return app
