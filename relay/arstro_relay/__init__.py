"""Arstro device manager (the relay): devices register here, viewers reach them through it.

The relay forwards viewer <-> device WebSocket messages verbatim and never sees an access
key or a decrypted byte. Protocol: docs/relay/PROTOCOL.md. Standard library only.

    python3 -m arstro_relay                      serve (configuration from RELAY_* variables)
    python3 -m arstro_relay admin list|forget ID|token [--rotate]
"""

__version__ = "1.1.1"
