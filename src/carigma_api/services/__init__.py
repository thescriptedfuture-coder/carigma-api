"""Stateless business-logic services.

Everything here is `service(input) -> output`: no request objects, no session
state, no UI. That is the whole point of the extraction — the same functions run
from an HTTP route, a cron job, or a test.

The prompts and algorithms are ported from V1's `resumeiq/` package **verbatim**.
They are the IP; rewriting them would silently change behaviour users depend on.
"""
