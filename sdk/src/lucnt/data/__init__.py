"""Packaged data files (domain content).

A real package rather than a namespace directory: `importlib.resources.files()`
is only reliably resolvable for an importable package, and this must work from
inside a wheel where there is no filesystem directory to walk.
"""
