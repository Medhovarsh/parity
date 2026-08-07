"""Allow ``python -m parity`` as an alternative to the console script."""

from parity.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
