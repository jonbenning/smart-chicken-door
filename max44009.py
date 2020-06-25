"""
MicroPython driver :
https://github.com/rcolistete/MicroPython_MAX44009
for Maxim Integrated MAX44009 ambient light I2C sensor :
https://www.maximintegrated.com/en/products/sensors/MAX44009.html
Version: 0.1.0 @ 2018/06/02
Author: Roberto Colistete Jr. (roberto.colistete at gmail.com)
License: MIT License (https://opensource.org/licenses/MIT)
"""

from micropython import const


MAX44009_I2C_DEFAULT_ADDRESS = const(0x4A)

_MAX44009_REG_CONFIGURATION = const(0x02)
_MAX44009_REG_LUX_HIGH_BYTE = const(0x03)
_MAX44009_REG_LUX_LOW_BYTE  = const(0x04)
 
MAX44009_REG_CONFIG_CONTMODE_DEFAULT     = const(0x00)   # Default mode, low power, measures only once every 800ms regardless of integration time
MAX44009_REG_CONFIG_CONTMODE_CONTINUOUS  = const(0x80)   # Continuous mode, readings are taken every integration time
MAX44009_REG_CONFIG_MANUAL_OFF           = const(0x00)   # Automatic mode with CDR and Integration Time are are automatically determined by autoranging
MAX44009_REG_CONFIG_MANUAL_ON            = const(0x40)   # Manual mode and range with CDR and Integration Time programmed by the user
MAX44009_REG_CONFIG_CDR_NODIVIDED        = const(0x00)   # CDR (Current Division Ratio) not divided, all of the photodiode current goes to the ADC
MAX44009_REG_CONFIG_CDR_DIVIDED          = const(0x08)   # CDR (Current Division Ratio) divided by 8, used in high-brightness situations
MAX44009_REG_CONFIG_INTRTIMER_800        = const(0x00)   # Integration Time = 800ms, preferred mode for boosting low-light sensitivity
MAX44009_REG_CONFIG_INTRTIMER_400        = const(0x01)   # Integration Time = 400ms
MAX44009_REG_CONFIG_INTRTIMER_200        = const(0x02)   # Integration Time = 200ms
MAX44009_REG_CONFIG_INTRTIMER_100        = const(0x03)   # Integration Time = 100ms, preferred mode for high-brightness applications
MAX44009_REG_CONFIG_INTRTIMER_50         = const(0x04)   # Integration Time = 50ms, manual mode only
MAX44009_REG_CONFIG_INTRTIMER_25         = const(0x05)   # Integration Time = 25ms, manual mode only
MAX44009_REG_CONFIG_INTRTIMER_12_5       = const(0x06)   # Integration Time = 12.5ms, manual mode only
MAX44009_REG_CONFIG_INTRTIMER_6_25       = const(0x07)   # Integration Time = 6.25ms, manual mode only
 

class MAX44009:
    
    def __init__(self, i2c, address=MAX44009_I2C_DEFAULT_ADDRESS):
        self.i2c = i2c
        self.address = address
        self.configuration = MAX44009_REG_CONFIG_CONTMODE_DEFAULT | MAX44009_REG_CONFIG_MANUAL_OFF

    @property
    def configuration(self):
        return self._config

    @configuration.setter
    def configuration(self, value):
        #self._config = value
        self._config = bytearray(value)
        self.i2c.writeto_mem(self.address, _MAX44009_REG_CONFIGURATION, self._config)

    @property
    def illuminance_lux(self):
        data = self.i2c.readfrom_mem(self.address, _MAX44009_REG_LUX_HIGH_BYTE, 2)
        exponent = (data[0] & 0xF0) >> 4
        mantissa = ((data[0] & 0x0F) << 4) | (data[1] & 0x0F)
        illuminance = ((2 ** exponent) * mantissa) * 0.045
        return illuminance   # float in lux
