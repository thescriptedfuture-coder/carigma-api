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
        self._op: str | None = None
        self._payload: Any = None
        self._conflict: str | None = None
        self._single = False
        self._order: tuple[str, bool] | None = None

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
        return all(str(row.get(k)) == str(v) for k, v in self._filters.items())

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
