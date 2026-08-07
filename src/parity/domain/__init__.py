"""Domain layer: pure data and pure functions.

Nothing in this package performs I/O, reads the environment, or imports an
adapter. That constraint is what makes the classification logic testable without
a network, a filesystem, or a paid API key.
"""
