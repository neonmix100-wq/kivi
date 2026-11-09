"""Allow ``python -m kivi``."""

import sys

from kivi.interfaces.cli import main

if __name__ == "__main__":
    sys.exit(main())
