"""Arstro backup terminal (ADM-09, ADM-10): a terminal-only server on its own port (8181) with its
own copy of the code, password, start-at-boot service and device-manager identity. It is installed
once (`install.sh --backup`) and never touched by installing or updating Arstro Remote's slots, so
the machine stays reachable when the main server is down, broken or being updated.

Its own code is this package; the parts it shares with Arstro Remote (the relay agent, its crypto
and transports, the WebSocket code, xterm) are COPIED into vendor/ and static/ when it is installed,
so a later update of the main server cannot change it.
"""

VERSION = "1.0.0"
