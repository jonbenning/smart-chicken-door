from machine import Pin
from machine import I2C
from machine import deepsleep
from machine import reset
import utime
import ntptime
from time import sleep, sleep_ms
import sys
import os
import max44009
import bme280_float as bme280
import esp32
import network
import _thread
import urequests
import json
import gc
import logging
import micropython


class ChickenDoor:
  def __init__(self):
    # Enable garbage collection
    gc.enable()
    _thread.stack_size(8192)

    # setup pins for esp32-32s
    self.led = Pin(2,Pin.OUT)
    self.en = Pin(14,Pin.OUT)
    self.m1 = Pin(27,Pin.OUT)
    self.m2 = Pin(26,Pin.OUT)
    # Determines if the door is in auto mode or manual
    self.mode_switch = Pin(25,Pin.IN,Pin.PULL_UP)
    # close_limit stops the motor when closing - normally closed
    self.close_limit = Pin(32,Pin.IN,Pin.PULL_UP)
    # open_limit stops the motor when opening - normally closed
    self.open_limit = Pin(33,Pin.IN,Pin.PULL_UP)
    # obstruction_limit stops the motor while closing, but before
    # the close limit. in case theres an obstruction. The motor mount will flex
    # and touch the switch. Copied from the "ladies first" door.- normally closed
    self.obstruction_limit = Pin(35,Pin.IN,Pin.PULL_UP)
    self.manual_open = Pin(15,Pin.IN,Pin.PULL_UP)
    self.manual_close = Pin(4,Pin.IN,Pin.PULL_UP)
    self.i2c = I2C(scl=Pin(5), sda=Pin(18))

    if self.mode_switch.value() == 0:
      self.mode = "auto"
    elif self.mode_switch.value() == 1:
      self.mode = "manual"


    _thread.start_new_thread(self.mode_monitor,())

    self.load_config()
    self.setup_logger()
    self.blink_freq = 0.1
    self.operation = None
    self.next_operation_time = None

    _thread.start_new_thread(self.blink,())
    if self.mode == "manual":
      _thread.start_new_thread(self.input_monitor,())

    self.wifi_connect()

    if self.mode == "auto":
      #Set the RTC to NTP...
      while True:
        try:
          ntptime.settime()
          break
        except:
          print("error setting RTC. Retrying...")
          sleep(1)
  

      #Set the sunrise/sunset attributes
      _thread.start_new_thread(self.get_sunrise_sunset,())
      gc.collect()

      #Start the thread to watch the clock
      _thread.start_new_thread(self.time_monitor,())

    # check for a state file and set
    self.target = self.get_target_state()

  def setup_logger(self):
    logging.basicConfig(level=logging.INFO)
    self.log = logging.getLogger("ChickenDoor")
 
  def blink(self):
    while True:
      if self.blink_freq:
        self.led.on()
        sleep(self.blink_freq)
        self.led.off()
        sleep(self.blink_freq)
      else:
        self.led.off()

  def mode_monitor(self):
    while True:
      if self.mode == "manual":
        if self.mode_switch.value() == 0:
          reset()
      elif self.mode == "auto":
        if self.mode_switch.value() == 1:
          reset()
      sleep(1)
        

  def time_monitor(self):
    while True:
      if self.next_operation_time:
        door_status = self.check_limits()
        if utime.time() > self.next_operation_time:
          if self.next_operation == "open":
            if door_status['actual'] != "open":
              self.open()
          elif self.next_operation == "close":
            if door_status['actual'] != "closed":
              self.close()
          else:
            # This shouldnt happen. its here for completeness
            print("errmagherd, something is wrong")

        else:
          #print("not time to open/close the door, but validate its the opposite of the next operation")
          time_till_operation = ((self.next_operation_time - utime.time()))
          minutes_total = int(time_till_operation / 60)
          sec_remainder = int(time_till_operation % 60)
          hours_until = int(minutes_total / 60)
          min_remainer = int(minutes_total % 60)

          print("Its {0}:{1}:{2} until the next operation".format(hours_until,min_remainer,sec_remainder))
          if self.next_operation == "open":
            if door_status['actual'] != "closed":
              # The door should be shut right now! Close it!
              self.close()
          elif self.next_operation == "close":
            if door_status['actual'] != "open":
              # The door should be open right now! Open it!
              self.open()
        sleep(60)
      else:
        print("waiting for sunset/sunrise data...")
        sleep(1)

  def convert_api_time(self,datestring):
    year,month,day = map(int, datestring.split("T")[0].split("-"))
    hours,minutes,seconds = map(int, datestring.split("T")[1].split("+")[0].split(":"))
    dateseconds = utime.mktime((year,month,day,hours,minutes,seconds,0,0))
    return dateseconds

  def api_request(self,day):
    print("Querying sunrise-sunset.org for {0}".format(day))
    print("mem before request: {0}".format(gc.mem_free()))
    api_url = "https://api.sunrise-sunset.org/json?lat={0}&lng={1}&formatted=0&date={2}".format(self.lat,self.lng,day)
    if self.sta_if.isconnected():
      response = urequests.get(url=api_url)
      print("mem after request: {0}".format(gc.mem_free()))
      gc.collect()
      return response.json()
    else:
      while True:
        #wait for the connection fully activate
        print("Waiting for connection to activate...")
        if  self.sta_if.isconnected():
          response = urequests.get(url=api_url)
          print("mem after request: {0}".format(gc.mem_free()))
          return response.json()
        #sleep(1)
      


  def get_sunrise_sunset(self):
    #while True:
      days = ("yesterday","today","tomorrow")

      response_dict = {}
      sunrise_sunset_dict = {}
      for day in days:
        response_dict[day] = self.api_request(day)

      for day in response_dict:
        sunrise_sunset_dict['{0}_sunrise'.format(day)] = self.convert_api_time(response_dict[day]['results']['sunrise'])
        sunrise_sunset_dict['{0}_sunset'.format(day)] = self.convert_api_time(response_dict[day]['results']['sunset'])

      # save this without current time for use elsewhere in the program
      self.sunrise_dict = sunrise_sunset_dict

      sunrise_sunset_dict['current'] = utime.time()

      sorted_dates = sorted(sunrise_sunset_dict.values())
      next_operation_index = sorted_dates.index(sunrise_sunset_dict['current'])+1
      next_operation_time = sorted_dates[next_operation_index]

      for name,datestamp in sunrise_sunset_dict.items():
        if datestamp == next_operation_time:
          print(name)
          if name.endswith("sunrise"):
            ## Open the door 2h after sunrise. allowing time for the chickens to lay eggs and stuff.
            self.next_operation = "open"
            self.next_operation_time = (next_operation_time + self.sunrise_offset)
          elif name.endswith("sunset"):
            ## close the door 10m before sunset
            self.next_operation = "close"
            self.next_operation_time = (next_operation_time + self.sunset_offset)

      ## Sleep for 6 hours before updating the sunset/sunrise data
      #sleep(14400)




  def load_config(self):
    with open("config.json","r") as w:
      json_string = w.read()
      json_config = json.loads(json_string)
      self.ssid = json_config['wifi']['ssid']
      self.passphrase = json_config['wifi']['passphrase']
      self.lat = json_config['location']['lat']
      self.lng = json_config['location']['lng']
      self.sunrise_offset = int(json_config['time']['sunrise_offset'])
      self.sunset_offset = int(json_config['time']['sunset_offset'])
      self.app_token = json_config['pushover']['app_token']
      self.group_key = json_config['pushover']['group_key']

  def wifi_connect(self):
    #with open("ap_config.txt","r") as w:
    #  ap_config = w.read()
    #ap_name,ap_password = ap_config.split(",")
      
    ap_name = self.ssid
    ap_password = self.passphrase
    print("{0}".format(ap_name.strip()))
    print("{0}".format(ap_password.strip()))
    self.sta_if = network.WLAN(network.STA_IF)
    self.sta_if.active(True)
    self.sta_if.scan()                             # Scan for available access points
    self.sta_if.connect("{0}".format(ap_name.strip()), "{0}".format(ap_password.strip())) # Connect to an AP
    self.sta_if.isconnected()                      # Check for successful connection
    while True:
      #wait for the connection fully activate
      print("Waiting for connection to activate...")
      if  self.sta_if.isconnected():
        break
      sleep(1)
    print(self.sta_if.ifconfig())
    sleep(5)

  def reset_state(self):
    try:
      os.remove("state.txt")
      print("removing state.txt")
    except:
      pass


  def read_switches(self):
      open1 = self.manual_open.value()
      close1 = self.manual_close.value()
      utime.sleep(0.02)
      open2 = self.manual_open.value()
      close2 = self.manual_close.value()

      return open1,close1,open2,close2


  def close(self,duration=None,attempt=0):
    gc.collect()
    if duration:
      close_time = utime.time() + duration
    else:
      open_time = None
    self.log.info("Close the door")
    sleep(0.5)
    with open("state.txt",'w',encoding = 'utf-8') as f:
      f.write("closed")
    self.m1.value(1)
    self.m2.value(0)
    if self.close_limit.value() == 1:
      self.log.info("Door is already closed!")
      return
    else:
      self.operation = "close"
      self.en.value(1)
    while True:
      self.log.info("closing the door...")

      ## Monitor buttons for input!!! ##
      open1,close1,open2,close2 = self.read_switches()

      if open1 != open2:
        self.log.info("Close operation was manually interrupted!")
        break

      if close1 != close2:
        self.log.info("Close operation was manually interrupted!")
        break

      ##################################

      ## if an obstuction is encountered, back off and retry, up to 3 times.
      ## then just open.TODO: send push notification!
      if self.obstruction_limit.value() == 1:
        self.en.value(0)
        self.log.info("Obstruction encountered! back off the door a little!")
        
        if attempt < 1:
          self.open(duration=3)
          attempt += 1
          self.close(attempt=attempt)
        else:
          #print("Sending notification...")
          _thread.start_new_thread(self.send,(self.app_token,self.group_key,"Check the door!"))
          self.open()
          
        return 1 # rc 1 means the obstruction switch was tripped

      if duration:
        if utime.time() >= close_time:
          self.log.info("close duration elapsed")
          break


      if self.close_limit.value() == 1:
        self.en.value(0)
        self.log.info("Door Closed!")
        #print("Sending notification...")
        _thread.start_new_thread(self.send,(self.app_token,self.group_key,"Door Closed!"))
        break

    self.en.value(0)
    self.operation = None
    sleep(0.5)
    return 0 # rc 0 means the door was shut

  def open(self,duration=None):
    gc.collect()
    if duration:
      open_time = utime.time() + duration
    else:
      open_time = None
    self.log.info("Open the door")
    sleep(0.5)
    with open("state.txt",'w',encoding = 'utf-8') as f:
      f.write("open")
    self.m1.value(0)
    self.m2.value(1)
    if self.open_limit.value() == 1:
      self.log.info("Door is already open!")
      return
    else:
      self.operation = "open"
      self.en.value(1)
    
    while True:
      self.log.info("opening the door")


      ## Monitor buttons for input!!! ##
      open1,close1,open2,close2 = self.read_switches()

      if open1 != open2:
        self.log.info("Open operation was manually interrupted!")
        break

      if close1 != close2:
        self.log.info("Open operation was manually interrupted!")
        break

      ##################################

      if duration:
        if utime.time() >= open_time:
          self.log.info("open duration elapsed")
          break

      if self.open_limit.value() == 1:
        self.en.value(0)
        self.log.info("Door Opened!")
        #print("Sending notification...")
        _thread.start_new_thread(self.send,(self.app_token,self.group_key,"Door Opened!"))
        break

    self.en.value(0)
    self.operation = None
    sleep(0.5)
    return


  def send(self,token,user,message):
    gc.collect()
    while True:
      try:
        pushover_url = "https://api.pushover.net/1/messages.json"
        headers = {'Content-Type': 'application/json'}
        json_data = json.dumps({'token': token,"user": user,"message": message})
        response = urequests.post(url=pushover_url,headers=headers,data=json_data)
        return response
        break
      except:
        print(micropython.mem_info())
        sleep(5)


  def input_monitor(self):
    print("Started monitoring for user input")
    self.log.info("Started monitoring for user input")
    while True:
      open1,close1,open2,close2 = self.read_switches()

      open_press = None
      close_press = None
      
      if open1 != open2:
        open_press = True
      else:
        open_press = False
    
      if close1 != close2:
        close_press = True
      else:
        close_press = False
     
      if open_press and not close_press:
        self.open()
      elif close_press and not open_press:
        self.close()
      elif open_press and close_press:
        self.reset_state()

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
        # Its not really possible that both switches are open unless theres a broken wire
        sys.exit()
      else:
        if self.target == "closed":
          return {"target":"closed","actual": "unknown"}
        elif self.target == "open":
          return {"target":"open","actual": "unknown"}

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
door.blink_freq = 0.5

print(door.read_sensors())
print(door.check_limits())

while True:
  # Waiting for things to happen
  gc.collect()
  gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
  sleep(5)

