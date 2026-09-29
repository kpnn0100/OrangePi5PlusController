"""Orange Pi 5 Plus (Rockchip RK3588): the 40-pin header.

GPIO numbers follow the Rockchip scheme: GPIO<bank>_<group><n> is line group*8+n of chip
"gpio<bank>" (global number bank*32 + line). The alternate functions come from the board's
device tree (pinctrl groups); a function works once its overlay is enabled in
/boot/orangepiEnv.txt (e.g. `overlays=i2c2-m0 uart3-m1 spi0-m2-cs0-spidev pwm14-m2`) and the
board is rebooted.
"""

NAME = "Orange Pi 5 Plus"
COMPATIBLE = ["rockchip,rk3588-orangepi-5-plus"]
GPIO_CHIP_LABEL = "gpio{bank}"
ENABLE_HINT = ("Add the overlay (for example i2c2-m0, uart3-m1, spi0-m2-cs0-spidev, pwm14-m2) to "
               "'overlays=' in /boot/orangepiEnv.txt and reboot.")


def _g(bank, group, n):
    return (bank, "ABCD".index(group) * 8 + n)


# (physical pin, name, gpio, alternate functions) - power pins have gpio None.
# Generated from the pinctrl groups of rk3588-orangepi-5-plus.dtb (roles in dtsi order:
# I2C scl,sda - UART rx,tx - SPI clk,miso,mosi - CAN rx,tx).
_PINS = [
    (1, "3V3", None, []), (2, "5V", None, []),
    (3, "GPIO0_C0", _g(0, "C", 0), ['I2C2_M0_SDA', 'SPI0_M0_MOSI', 'PWM1_M0', 'CAN0_M0_RX']),
    (4, "5V", None, []),
    (5, "GPIO0_B7", _g(0, "B", 7), ['I2C2_M0_SCL', 'SPI0_M0_CS1', 'PWM0_M0', 'CAN0_M0_TX']),
    (6, "GND", None, []),
    (7, "GPIO1_D6", _g(1, "D", 6), ['I2C8_M2_SCL', 'PWM14_M2']),
    (8, "GPIO1_A1", _g(1, "A", 1), ['I2C2_M4_SCL', 'UART6_M1_TX', 'SPI4_M2_MOSI']),
    (9, "GND", None, []),
    (10, "GPIO1_A0", _g(1, "A", 0), ['I2C2_M4_SDA', 'UART6_M1_RX', 'SPI4_M2_MISO']),
    (11, "GPIO1_A4", _g(1, "A", 4), ['SPI2_M0_MISO']),
    (12, "GPIO3_A1", _g(3, "A", 1), ['I2C6_M4_SCL', 'SPI4_M1_MOSI', 'PWM11_M0']),
    (13, "GPIO1_A7", _g(1, "A", 7), ['SPI2_M0_CS0', 'PWM3_M3']),
    (14, "GND", None, []),
    (15, "GPIO1_B0", _g(1, "B", 0), ['SPI2_M0_CS1']),
    (16, "GPIO3_B5", _g(3, "B", 5), ['UART3_M1_TX', 'PWM12_M0', 'CAN1_M0_RX']),
    (17, "3V3", None, []),
    (18, "GPIO3_B6", _g(3, "B", 6), ['UART3_M1_RX', 'PWM13_M0', 'CAN1_M0_TX']),
    (19, "GPIO1_B2", _g(1, "B", 2), ['UART4_M2_RX', 'SPI0_M2_MOSI']),
    (20, "GND", None, []),
    (21, "GPIO1_B1", _g(1, "B", 1), ['SPI0_M2_MISO']),
    (22, "GPIO1_A2", _g(1, "A", 2), ['I2C4_M3_SDA', 'SPI4_M2_CLK', 'PWM0_M2']),
    (23, "GPIO1_B3", _g(1, "B", 3), ['UART4_M2_TX', 'SPI0_M2_CLK']),
    (24, "GPIO1_B4", _g(1, "B", 4), ['UART7_M2_RX', 'SPI0_M2_CS0']),
    (25, "GND", None, []),
    (26, "GPIO1_B5", _g(1, "B", 5), ['UART7_M2_TX', 'SPI0_M2_CS1']),
    (27, "GPIO1_B7", _g(1, "B", 7), ['I2C5_M3_SDA', 'UART1_M1_RX', 'PWM13_M2']),
    (28, "GPIO1_B6", _g(1, "B", 6), ['I2C5_M3_SCL', 'UART1_M1_TX']),
    (29, "GPIO1_D7", _g(1, "D", 7), ['I2C8_M2_SDA', 'PWM15_M3']),
    (30, "GND", None, []),
    (31, "GPIO3_A0", _g(3, "A", 0), ['I2C6_M4_SDA', 'SPI4_M1_MISO', 'PWM10_M0']),
    (32, "GPIO1_A3", _g(1, "A", 3), ['I2C4_M3_SCL', 'SPI4_M2_CS0', 'PWM1_M2']),
    (33, "GPIO3_C2", _g(3, "C", 2), ['I2C8_M4_SCL', 'SPI1_M1_CS0', 'PWM14_M0']),
    (34, "GND", None, []),
    (35, "GPIO3_A2", _g(3, "A", 2), ['UART8_M1_TX', 'SPI4_M1_CLK']),
    (36, "GPIO3_A5", _g(3, "A", 5), ['I2C4_M0_SDA']),
    (37, "GPIO3_C1", _g(3, "C", 1), ['UART7_M1_RX', 'SPI1_M1_CLK']),
    (38, "GPIO3_A4", _g(3, "A", 4), ['SPI4_M1_CS1']),
    (39, "GND", None, []),
    (40, "GPIO3_A3", _g(3, "A", 3), ['UART8_M1_RX', 'SPI4_M1_CS0']),
]

HEADER = []
for _pin, _name, _gpio, _alt in _PINS:
    entry = {"pin": _pin, "name": _name, "alt": _alt}
    if _gpio:
        entry["gpio"] = {"bank": _gpio[0], "line": _gpio[1], "number": _gpio[0] * 32 + _gpio[1]}
    else:
        entry["power"] = _name
    HEADER.append(entry)
