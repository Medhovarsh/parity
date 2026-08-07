"""Security controls applied to captured payloads and untrusted files.

Two jobs:

* :mod:`parity.security.redaction` strips credentials and personal data from a
  payload **before** it is ever written to a store. Baselines get committed,
  shared, and attached to tickets; they must not carry secrets with them.
* :mod:`parity.security.limits` bounds what a hostile or corrupt file can do to
  the process reading it. Rejecting loudly beats exhausting memory.

See ``SECURITY.md`` for the threat model these implement.
"""

from parity.security.limits import Limits, guard_depth, guard_file_size, harden_permissions
from parity.security.redaction import RedactionReport, Redactor, default_redactor

__all__ = [
    "Limits",
    "RedactionReport",
    "Redactor",
    "default_redactor",
    "guard_depth",
    "guard_file_size",
    "harden_permissions",
]
