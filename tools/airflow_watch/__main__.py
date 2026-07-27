"""Allow `python -m tools.airflow_watch`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
