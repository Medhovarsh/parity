"""Adapters: concrete implementations of the ports.

Nothing in :mod:`parity.domain`, :mod:`parity.checks`, or
:mod:`parity.classify` may import from this package. Selection happens at the
edge — in the CLI, or in an application embedding the library.
"""
