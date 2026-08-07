"""Capture: turning existing logs into a behaviour baseline.

The product's central bet is that you should not have to author test cases. You
already have the inputs and the outputs — they are in your logs. Capture reads
them, normalises the several shapes they arrive in, redacts them, and stores
them as cases.
"""

from parity.capture.importer import ImportResult, import_records, parse_record
from parity.capture.recorder import Recorder

__all__ = ["ImportResult", "Recorder", "import_records", "parse_record"]
