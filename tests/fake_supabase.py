"""One in-memory Supabase for every route test.

There were two of these and the weekly port wanted a third. Three fakes of the
same thing is where they drift: each grows the behaviour its own tests need,
and eventually one of them models a write the real client does not do.

## What it deliberately models

**`update` applies its filters and returns only the rows it changed.** That
affected-row count is the entire idempotency mechanism behind `claim_payment`
and `claim_cycles` — a fake where `update()` always reported success would let
the V1 double-credit bug through while looking green.

**`upsert` needs `on_conflict`.** A fake that appended unconditionally would
hide a duplicate-row bug: two contracts for one week make `consecutive_lapses`
see a history that never happened.

**Failures are per-table.** `db.failing.add("credits")` makes exactly that
table raise, which is how "one source is down" gets tested without taking the
whole request with it.

It is NOT a Postgres. No constraints, no RLS, no type coercion — a test that
needs those needs the real database.
"""

from __future__ import annotations

from typing import Any


class Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeTable:
    def __init__(self, db: FakeDB, name: str) -> None:
        self._db = db
        self._name = name
        self._filters: dict[str, Any] = {}
        self._in: dict[str, list[str]] = {}
        self._op: str | None = None
        self._payload: Any = None
        self._conflict: str | None = None
        self._single = False
        self._order: tuple[str, bool] | None = None
        self._ranges: list[tuple[str, str, str]] = []

    # ── builders ───────────────────────────────────────────────────────────

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def insert(self, payload: Any) -> FakeTable:
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload: Any, **kw: Any) -> FakeTable:
        self._op, self._payload = "upsert", payload
        self._conflict = kw.get("on_conflict")
        return self

    def delete(self) -> FakeTable:
        self._op = "delete"
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def in_(self, column: str, values: list[Any]) -> FakeTable:
        """`WHERE column IN (...)`. Production calls this in `routes/jobs.py`
        and the fake never had it — found by asking, for every method our code
        calls on a table chain, whether the stand-in answers it."""
        self._in[column] = [str(v) for v in values]
        return self

    def gte(self, column: str, value: Any) -> FakeTable:
        """`>=`, compared as STRINGS.

        Every caller passes an ISO timestamp, which sorts lexicographically in
        the same order it sorts chronologically — so a string compare is the
        right one here and a date parse would only add a way to be wrong.
        Anything non-ISO would compare nonsensically, so callers pass ISO.
        """
        self._ranges.append((column, ">=", str(value)))
        return self

    def lte(self, column: str, value: Any) -> FakeTable:
        self._ranges.append((column, "<=", str(value)))
        return self

    def order(self, column: str, **kw: Any) -> FakeTable:
        self._order = (column, bool(kw.get("desc", False)))
        return self

    def limit(self, _n: int) -> FakeTable:
        return self

    def maybe_single(self) -> FakeTable:
        self._single = True
        return self

    # ── execution ──────────────────────────────────────────────────────────

    def _matches(self, row: dict[str, Any]) -> bool:
        if not all(str(row.get(k)) == str(v) for k, v in self._filters.items()):
            return False
        for column, op, value in self._ranges:
            actual = str(row.get(column) or "")
            if op == ">=" and not actual >= value:
                return False
            if op == "<=" and not actual <= value:
                return False
        # `in_`, compared as STRINGS like `eq` above, so the two cannot
        # disagree about what "equal" means for the same column.
        for column, allowed in self._in.items():
            if str(row.get(column)) not in allowed:
                return False
        return True

    def __getattr__(self, name: str) -> Any:
        """Say WHICH method is missing, and that the real client has it.

        Two fakes have now been absent rather than simplified. `_NullRunStore`
        accepted every run and rejected none. This class's sibling in
        `test_payments_roundtrip` had no `upsert` at all — and the day
        `apply_delta` stopped using `update`, three payment tests failed with a
        bare AttributeError, in a suite about payments, for a reason that had
        nothing to do with payments.

        A STATIC "implements everything production calls" check was written and
        thrown away: most fakes stand in for two or three tables and would fail
        it for methods they will never be asked for, and a guard that fires on
        correct code gets deleted. The honest version is this — it cannot fire
        early, and when it does fire it names the cause.
        """
        raise AttributeError(
            f"{type(self).__name__} has no `{name}`, and production code just called it on a "
            "table chain. The real client implements it; this stand-in does not. Add it — and "
            "model what it REFUSES, not only what it accepts."
        )

    def execute(self) -> Result:
        if self._name in self._db.failing:
            raise RuntimeError(f"{self._name} is down")
        rows = self._db.tables.setdefault(self._name, [])

        if self._op == "insert":
            new = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(r) for r in new)
            return Result([dict(r) for r in new])

        if self._op == "upsert":
            assert self._conflict, "upsert without on_conflict — the real client needs one"
            keys = [k.strip() for k in self._conflict.split(",")]
            new = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for incoming in new:
                ident = tuple(str(incoming.get(k)) for k in keys)
                existing = next(
                    (r for r in rows if tuple(str(r.get(k)) for k in keys) == ident), None
                )
                if existing is None:
                    rows.append(dict(incoming))
                    out.append(dict(incoming))
                else:
                    # A partial upsert MERGES onto the stored row, as Postgres
                    # does — it does not blank the columns it omits.
                    existing.update(incoming)
                    out.append(dict(existing))
            return Result(out)

        if self._op == "update":
            hit = [r for r in rows if self._matches(r)]
            for row in hit:
                row.update(self._payload)
            return Result([dict(r) for r in hit])

        if self._op == "delete":
            gone = [r for r in rows if self._matches(r)]
            self._db.tables[self._name] = [r for r in rows if not self._matches(r)]
            return Result([dict(r) for r in gone])

        found = [dict(r) for r in rows if self._matches(r)]
        if self._order:
            column, desc = self._order
            found.sort(key=lambda r: str(r.get(column) or ""), reverse=desc)
        if self._single:
            return Result(found[0] if found else None)
        return Result(found)


class _Deferred:
    """`.rpc(...).execute()` — the builder shape the real client uses."""

    def __init__(self, run: Any) -> None:
        self._run = run

    def execute(self) -> Any:
        return self._run()


class FakeDB:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.failing: set[str] = set()
        self.functions: dict[str, Any] = {}

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        """Postgres functions, which some surfaces must use instead of tables.

        Registered per test with `db.functions[name] = fn`. Unregistered names
        raise rather than returning empty: a silent `[]` from a typo'd function
        name is indistinguishable from a real empty answer, and on the referral
        surface an empty answer is a valid-looking "you have earned nothing".
        """
        fn = self.functions.get(name)
        if fn is None:
            raise KeyError(f"no fake for rpc {name!r} — register it in db.functions")
        return _Deferred(lambda: Result(fn(params)))

    def rows(self, name: str) -> list[dict[str, Any]]:
        return self.tables.get(name, [])

    def seed(self, name: str, *rows: dict[str, Any]) -> None:
        self.tables.setdefault(name, []).extend(dict(r) for r in rows)


__all__ = ["FakeDB", "FakeTable", "Result"]
