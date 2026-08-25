"""Test package.

This file is load-bearing. Without it pytest imports `conftest.py` as the
top-level module `conftest`, while `from tests.conftest import ...` in a test
file imports a *second, separate* copy as `tests.conftest`. Module-level state
then exists twice — which silently broke signature verification once, because
the fixture signed tokens with one generated key while the JWKS stub served the
public half of the other.
"""
