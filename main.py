from machine import Pin
from machine import I2C
from machine import deepsleep
import time
from time import sleep, sleep_ms
import sys
import os
import max44009
import bme280_float as bme280
import esp32


class ChickenDoor:
  def __init__(self):
    # setup pins for esp32-32s
    self.en = Pin(14,Pin.OUT)
    self.m1 = Pin(27,Pin.OUT)
    self.m2 = Pin(26,Pin.OUT)
    # close_limit stops the motor when closing - normally closed
    self.close_limit = Pin(32,Pin.IN,Pin.PULL_UP)
    # open_limit stops the motor when opening - normally closed
    self.open_limit = Pin(33,Pin.IN,Pin.PULL_UP)
    self.manual_open = Pin(15,Pin.IN,Pin.PULL_UP)
    self.manual_close = Pin(4,Pin.IN,Pin.PULL_UP)
    self.i2c = I2C(scl=Pin(5), sda=Pin(18))
    
    # check for a state file and set
    self.target = self.get_target_state()
 
  def reset_state(self):
    try:
      os.remove("state.txt")
      print("removing state.txt")
    except:
      pass

  def close(self):
    print("Close the door")
    sleep(0.5)
    with open("state.txt",'w',encoding = 'utf-8') as f:
      f.write("closed")
    self.m1.value(1)
    self.m2.value(0)
    if self.close_limit.value() == 1:
      print("Door is already closed!")
      return
    else:
      self.en.value(1)
    while True:
      print("closing the door...")

      manual_open1 = self.manual_open.value()
      manual_close1 = self.manual_close.value()
      time.sleep(0.02)
      manual_open2 = self.manual_open.value()
      manual_close2 = self.manual_close.value()

      if manual_open1 != manual_open2:
        print("Close operation was manually interrupted!")
        break

      if manual_close1 != manual_close2:
        print("Close operation was manually interrupted!")
        break

      if self.close_limit.value() == 1:
        print("Door Closed!")
        break

    self.en.value(0)
    sleep(0.5)
    return

  def open(self):
    print("Open the door")
    sleep(0.5)
    with open("state.txt",'w',encoding = 'utf-8') as f:
      f.write("open")
    self.m1.value(0)
    self.m2.value(1)
    if self.open_limit.value() == 1:
      print("Door is already open!")
      return
    else:
      self.en.value(1)
    
    while True:
      print("opening the door")

      manual_open1 = self.manual_open.value()
      manual_close1 = self.manual_close.value()
      time.sleep(0.02)
      manual_open2 = self.manual_open.value()
      manual_close2 = self.manual_close.value()

      if manual_open1 != manual_open2:
        print("Open operation was manually interrupted!")
        break

      if manual_close1 != manual_close2:
        print("Open operation was manually interrupted!")
        break

      if self.open_limit.value() == 1:
        print("Door Opened!")
        break

    self.en.value(0)
    sleep(0.5)
    return

  def i2c_scan(self):
    devices = self.i2c.scan()
    
    if len(devices) == 0:
      print("No i2c device !")
    else:
      print('i2c devices found:',len(devices))
    
      for device in devices:  
        print("Decimal address: ",device," | Hexa address: ",hex(device))

  def read_sensors(self):
    lux_sensor = max44009.MAX44009(self.i2c)
    bme_sensor = bme280.BME280(i2c=self.i2c)

    #bme.sealevel = 101019
    values = bme_sensor.values
    sensor_data = {
      'lux': lux_sensor.illuminance_lux,
      'temperature': values[0],
      'pressure': values[1],
      'humidity': values[2]
    }
    return sensor_data

  def get_target_state(self):
    try:
      f = open("state.txt",'r',encoding= 'utf-8')
      target_state = f.read()
      if target_state == "open":
        return "open"
      elif target_state == "closed":
        return "closed"
      else:
        # any other value means something is probably corrupt and needs to be reset
        return None
    except OSError:
       # Couldnt read the file, Probably means it doesnt exist.
       return None


  def check_limits(self):
    '''
    One of the limit switches should be open,and one closed. Planning on using normally closed
    switches. If both are closed,that means the door is neither fully open nor fully closed. 
    if both are open, that condition should not be possible, and probably means some kind of fault.
    '''

    # read the values from the limit switches
    open_limit = self.open_limit.value()
    close_limit = self.close_limit.value()

    # simple check that the limits arent both open or both closed
    if close_limit != open_limit:
      if self.target != None:
        if self.target == "closed":
          if close_limit == 1:
            # the target state is closed and the limit switch says the door is closed.
            return {"target":"closed","actual":"closed"}
          else:
            # the target state is closed and the limit switch says the door is open
            return {"target":"closed","actual":"open"}
        elif self.target == "open":
          if open_limit == 1:
            # the target state is open and the limit switch says the door is open.
            return {"target":"open","actual":"open"}
          else:
            # the target state is open and the limit switch says the door is closed
            return {"target":"open","actual":"closed"}
      else:
        # This runs when there is no state data. probably newly flashed firmware,
        # or the door has been reset. The door should close by default...
        #self.close()
        return {"target":"unknown","actual":"closed"}
    else:
      # condition of both switches are closed. could happen if the door is reset
      # during an operation
      if open_limit == 1:
        if self.target == "closed":
          return {"target":"closed","actual": "unknown"}
        elif self.target == "open":
          return {"target":"open","actual": "unknown"}
      else:
        print("some kind of fault. both limits cannot be open or closed at the same time")
        sys.exit()

  def sync_state(self):
    '''
    Ensure the door matches the intended target state.
    If it doesn't, either open/close as the state.txt file
    states.
    '''
    door_status = self.check_limits()
    if door_status['target'] == "closed":
      if door_status['actual'] == "closed":
        # door is closed, and matches config. pass
        print("door is closed, and matches config.")
        pass
      elif door_status['actual'] == "open":
        # door isnt closed, and config says it should be, close it!
        print("door isnt closed, and config says it should be, close it!")
        self.close()
      else:
        # everything is awful. exit?!
       sys.exit()
    
    elif door_status['target'] == "open":
      if door_status['actual'] == "open":
        # door is open, and matches config. pass
        print("door is open, and matches config.")
        pass
      elif door_status['actual'] == "closed":
        # door isnt open, and config says it should be, open it!
        print("door isnt open, and config says it should be, open it!")
        self.open()
      else:
        # everything is awful. exit?!
        sys.exit()

    elif door_status['target'] == 'unknown':
      # This runs when there is no state data. probably newly flashed firmware,
      # or the door has been reset. The door should close by default...
      print("Target isn't defined. Closing the door as default.")
      self.close()


door = ChickenDoor()
print(door.read_sensors())
print(door.check_limits())
#door.sync_state()

timeout = time.time() + (20)

while time.time() <= timeout:
  manual_open1 = door.manual_open.value()
  manual_close1 = door.manual_close.value()
  time.sleep(0.02)
  manual_open2 = door.manual_open.value()
  manual_close2 = door.manual_close.value()
  open_press = None
  close_press = None
  
  if manual_open1 != manual_open2:
    open_press = True
  else:
    open_press = False

  if manual_close1 != manual_close2:
    close_press = True
  else:
    close_press = False


  if open_press and not close_press:
    door.open()
    # reset the timeout due to a button press
    timeout = time.time() + (20)
  elif close_press and not open_press:
    door.close()
    timeout = time.time() + (20)
  elif open_press and close_press:
    door.reset_state()
    timeout = time.time() + (20)
    

  #time.sleep(1)

#level parameter can be: esp32.WAKEUP_ANY_HIGH or esp32.WAKEUP_ALL_LOW
esp32.wake_on_ext0(pin = door.manual_open, level = esp32.WAKEUP_ALL_LOW)

# Couldnt get ext1 with two wakeup switches to work. leave it here for knowledge...
#esp32.wake_on_ext1(pins = (door.manual_open, door.manual_close), level = esp32.WAKEUP_ALL_LOW)

print('Going to sleep now')

###  1000 * 60 * 10 = 10m in milliseconds
sleepytime =  1000*60*10
deepsleep(sleepytime)
