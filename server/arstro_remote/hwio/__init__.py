"""Hardware IO through standard Linux interfaces (IO-01..10): GPIO character device,
i2c-dev, spidev, termios serial ports, sysfs PWM / LEDs / IIO ADC.

service.py exposes them as `io.*` ops; boards/ adds header pin names on known boards.
"""
