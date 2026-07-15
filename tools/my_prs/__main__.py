"""Allow `python -m tools.my_prs`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
