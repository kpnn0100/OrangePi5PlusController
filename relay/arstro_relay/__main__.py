"""python3 -m arstro_relay              run the device manager in the foreground
python3 -m arstro_relay admin ...    admin commands on the data directory (admin --help)
"""

import logging
import sys

from . import __version__

USAGE = """usage: python3 -m arstro_relay              run the device manager (settings: RELAY_* variables)
       python3 -m arstro_relay admin list | forget ID | token [--rotate]
       python3 -m arstro_relay --version"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["admin"]:
        from .admin import main as admin
        return admin(argv[1:])
    if argv == ["--version"]:
        print(__version__)
        return 0
    if argv:
        print(USAGE, file=sys.stdout if argv[0] in ("-h", "--help") else sys.stderr)
        return 0 if argv[0] in ("-h", "--help") else 2
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .server import Config, ConfigError, serve
    try:
        cfg = Config()
    except ConfigError as e:
        print("arstro_relay: %s" % e, file=sys.stderr)
        return 2
    return serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
