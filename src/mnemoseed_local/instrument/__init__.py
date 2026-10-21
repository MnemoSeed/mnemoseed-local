"""Isolated measurement instrument.

The package holds the instrument's own provenance surfaces: the append-only
ledger, the clock capture, the fixtures, the repair evidence, the admission
recomputation, the projection, and the daily CLI. It is not wired into the
product daemon and owns no runtime path of its own.

Slices import concrete modules; this package exports nothing.
"""
