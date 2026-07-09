"""Allow `python -m tools.slack_me`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
