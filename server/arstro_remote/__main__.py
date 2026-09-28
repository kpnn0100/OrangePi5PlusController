"""python3 -m arstro_remote ... -> the `arstro-remote` command (see cli.py)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
