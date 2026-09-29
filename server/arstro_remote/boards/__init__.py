"""Board-specific knowledge, kept out of the generic modules (MOD-03).

A board module only *adds information* to the standard interfaces: the pin header
(physical pin -> GPIO chip/line, alternate functions) and where to enable buses
(device-tree overlays). On an unknown board everything still works; the header view is
just missing.

A board module provides:
    NAME             human name
    COMPATIBLE       device-tree compatible strings it matches
    HEADER           [{"pin", "name", "gpio"?: (bank, offset), "alt": [...], "power"?: "3V3|5V|GND"}]
    GPIO_CHIP_LABEL  "gpio{bank}" - the chip label of a GPIO bank (as the kernel reports it)
    ENABLE_HINT      how to turn on an I2C/SPI/UART/PWM function of the header
"""

import importlib

from ..hwio import dt

BOARDS = ["orangepi5plus"]


def detect():
    """The board module for this machine, or None."""
    compat = dt.compatible()
    for name in BOARDS:
        mod = importlib.import_module("." + name, __name__)
        if any(c in compat for c in mod.COMPATIBLE):
            return mod
    return None


def describe(board):
    if board is None:
        return {"board": None, "model": dt.model(), "header": []}
    return {"board": board.NAME, "model": dt.model(), "header": board.HEADER,
            "chip_label": board.GPIO_CHIP_LABEL, "enable_hint": board.ENABLE_HINT}
