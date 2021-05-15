from machine import Pin
from machine import PWM
from machine import I2C
from machine import deepsleep
from machine import reset
from Suntime import Sun
import utime
import ntptime
from time import sleep, sleep_ms
from time import sleep, sleep_us
import sys
import os
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
    self.activity_led = Pin(19,Pin.OUT)
    self.activity_led.value(1)

    self.slp = Pin(14,Pin.OUT,None)
    self.stp = Pin(27,Pin.OUT) #step when stepper mode
    self.dir = Pin(26,Pin.OUT) #dir when stepper mode

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
    
    # Setup interrupts for the limit switches
    self.open_limit.irq(trigger=Pin.IRQ_RISING, handler=self.limit_handler)
    self.close_limit.irq(trigger=Pin.IRQ_RISING, handler=self.limit_handler)
    self.obstruction_limit.irq(trigger=Pin.IRQ_RISING, handler=self.limit_handler)


    self.manual_open = Pin(15,Pin.IN,Pin.PULL_UP)
    self.manual_close = Pin(4,Pin.IN,Pin.PULL_UP)


    if self.load_config():
      ## Config was successfully loaded
      # Check if the open button is being held at startup...
      if ((self.manual_open.value() == 0) and (self.manual_close.value() == 1)):
        self.update_config()
      ## holding the close button at startup puts the controller in diag mode.
      ## it will connect to wifi and drop to a repl prompt
      elif ((self.manual_open.value() == 1) and (self.manual_close.value() == 0)):
        self.wifi_connect()
        sys.exit()
        
      else:
        if self.mode_switch.value() == 0:
          self.mode = "auto"
        elif self.mode_switch.value() == 1:
          self.mode = "manual"

        if self.is_stepper:
          self.slp_status = False
          self.close_dir = True
          self.open_dir = False
        else:
          pass


        self.mode_switch.irq(trigger=Pin.IRQ_RISING|Pin.IRQ_FALLING, handler=self.mode_callback)

        self.setup_logger()
        self.blink_freq = 0.1
        self.operation = None
        self.next_operation_time = None

        _thread.start_new_thread(self.blink,())

        if self.mode == "manual":
          self.manual_open.irq(trigger=Pin.IRQ_FALLING, handler=self.input_handler)
          self.manual_close.irq(trigger=Pin.IRQ_FALLING, handler=self.input_handler)
          self.log.info("Started monitoring for user input")
          self.timeout = utime.time() + (60)
          while utime.time() <= self.timeout:
            sleep(1)

          self.standby()


        elif self.mode == "auto":
          #Connect to Wifi
          self.wifi_connect()
          #Set the RTC to NTP...
          while True:
            try:
              ntptime.settime()
              break
            except:
              self.log.info("Error setting RTC. Retrying...")
              sleep(1)
          gc.collect()
  

          #Set the sunrise/sunset attributes
          _thread.start_new_thread(self.get_sunrise_sunset,())

          #Start the thread to watch the clock
          _thread.start_new_thread(self.time_monitor,())

        # check for a state file and set
        self.target = self.get_target_state()
    else: 
      self.update_config()


  def input_handler(self,pin):

    if self.input_sense_time:
      pass
    else:
      self.input_sense_count += 1
      self.input_sense_time = utime.ticks_ms() + 5

    if ((utime.ticks_ms() < self.input_sense_time) and (self.input_sense_count > 1)):
      # this irq is detected during debounce time of 5ms
      pass
    else:
      if pin == self.manual_open:
        if self.manual_open.value() == 0:
          self.operation = "open"
          self.slp_status = not self.slp_status
          if self.slp_status:
            self.open(notify=False)
          else:
            self.disable_motor()

      elif pin == self.manual_close:
        if self.manual_close.value() == 0:
          self.operation = "close"
          self.slp_status = not self.slp_status
          if self.slp_status:
            self.close(notify=False)
          else:
            self.disable_motor()

      #enable_irq(self.input_irq_status)
      self.limit_sense_time = None
      self.limit_sense_count = 0
      self.timeout = utime.time() + (60)


  def limit_handler(self,pin):
    if self.limit_sense_time:
      pass
    else:
      self.limit_sense_count += 1
      self.limit_sense_time = utime.ticks_ms() + 5

    if ((utime.ticks_ms() < self.limit_sense_time) and (self.limit_sense_count > 1)):
      # this irq is detected during debounce time of 5ms
      pass
    else:
      if pin == self.open_limit:
        # The door is open, disable the driver!
        if self.operation == "open":
          self.disable_motor()
          self.log.info("Door has been opened")
          self.limit_sense_time = None
          if self.mode == "auto":
            if not self.notification_sent:
              #_thread.start_new_thread(self.send,(self.app_token,self.group_key,"Door Opened!"))
              self.send(self.app_token,self.group_key,"Door Opened!")
              self.notification_sent = True
      elif pin == self.close_limit:
        # The door is closed, disable the driver!
        if self.operation == "close":
          self.disable_motor()
          self.log.info("Door has been closed")
          self.limit_sense_time = None
          if self.mode == "auto":
            if not self.notification_sent:
              #_thread.start_new_thread(self.send,(self.app_token,self.group_key,"Door Closed!"))
              self.send(self.app_token,self.group_key,"Door Closed!")
              self.notification_sent = True
      elif pin == self.obstruction_limit:
        if self.obstruction_limit.value() == 1:
          if self.close_attempts < 2:
            self.limit_sense_time = None
            # The door encountered an obstruction while closing!
            # disable the driver, change direction, and reenable? maybe just change direction??
            self.log.info("Hit an obstruction")
            self.disable_motor()
            self.dir.value(not self.dir.value())
            self.enable_motor()
            sleep(3)
            self.disable_motor()
            self.dir.value(not self.dir.value())
            self.enable_motor()
            self.close_attempts += 1
          else:
            if self.mode == "auto":
              self.disable_motor()
              if not self.notification_sent:
                #self.send(self.app_token,self.group_key,"!!! Check the door !!!",1)
                _thread.start_new_thread(self.send,(self.app_token,self.group_key,"!!! Check the door !!!",1))
                self.notification_sent = True
              
            self.limit_sense_time = None
            self.open()

  def disable_motor(self):
    self.pending_operation = False
    self.pending_operation_time = 0

    motor_freq = self.motor_max
    if getattr(self,"pwm",None):
      while motor_freq > self.motor_min:
        self.pwm.freq(motor_freq)
        motor_freq -= self.motor_ramp_steps
        sleep_ms(self.motor_ramp_time)

    if getattr(self,"pwm",None):
      self.pwm.deinit()

    self.slp.value(0)

  def enable_motor(self):
    self.slp.value(1)
    #self.pwm = PWM(self.stp, freq=1200)
    motor_freq = self.motor_min
    self.pwm = PWM(self.stp, freq=motor_freq)
    while motor_freq < self.motor_max:
      self.pwm.freq(motor_freq)
      motor_freq += self.motor_ramp_steps
      sleep_ms(self.motor_ramp_time)
    self.pending_operation = True
    self.pending_operation_time = utime.time()
    if self.operation == "close":
      self.log.info("closing the door...")
    elif self.operation == "open":
      self.log.info("opening the door...")

  def build_html_form(self,message=""):
    config = {} 
    if getattr(self,"json_config",None):
      if self.json_config.get('wifi',None):
        ssid = self.json_config['wifi'].get('ssid',"")
        passphrase = self.json_config['wifi'].get('passphrase',"")
      else:
        ssid = ""
        passphrase = ""
 
      if self.json_config.get('location',None):
        lat = self.json_config['location'].get('lat',"")
        lng = self.json_config['location'].get('lng',"")
      else:
        lat = ""
        lng = ""

      if self.json_config.get('time',None):
        sunrise_offset = int(self.json_config['time'].get('sunrise_offset',"0"))
        sunset_offset = int(self.json_config['time'].get('sunset_offset',"0"))
      else:
        sunrise_offset = "0"
        sunset_offset = "0"

      if self.json_config.get('pushover',None):
        app_token = self.json_config['pushover'].get('app_token',"")
        group_key = self.json_config['pushover'].get('group_key',"")
      else:
        app_token = ""
        group_key = ""

      if self.json_config.get('motor_tuning',None):
        motor_min = int(self.json_config['motor_tuning'].get('motor_min',"500"))
        motor_max = int(self.json_config['motor_tuning'].get('motor_max',"1100"))
        ramp_time = int(self.json_config['motor_tuning'].get('ramp_time',"5"))
        ramp_steps = int(self.json_config['motor_tuning'].get('ramp_steps',"10"))
      else:
        motor_min = 500
        motor_max = 1100
        ramp_time = 5
        ramp_steps = 10

    else:
      ssid = ""
      passphrase = ""
      lat = ""
      lng = ""
      sunrise_offset = "0"
      sunset_offset = "0"
      app_token = ""
      group_key = ""


    html_list = [
    "<!DOCTYPE html>",
    "<html>",
    "<head>",
      "<title>Update Chicken Coop Config</title>",
    "</head>",
    "<link rel='icon' href='data:;base64,='>  <!-- para evitar 2 conexiones, http y favicon -->",
    "<center><h2>Chicken Coop Config</h2></center>",
    "<form action='/' method='POST'><center>",
      "<table cellspacing='5px' cellpadding='5%' align='center'>",
        "<tr>",
          "<td align='right'>Wireless SSID:</td>",
          "<td><input type='text' name='ssid' placeholder='ssid' value='{0}'></td>".format(ssid),
        "</tr>",
        "<tr>",
          "<td align='right'>Wireless Passphrase:</td>",
          "<td><input type='text' name='passphrase' placeholder='Wifi passphrase' value='{0}'></td>".format(passphrase),
        "</tr>",
        "<tr>",
          "<td align='right'>Latitude:</td>",
          "<td><input type='text' name='lat' placeholder='Decimal Latitude' value='{0}'></td>".format(lat),
        "</tr>",
        "<tr>",
          "<td align='right'>Longitude:</td>",
          "<td><input type='text' name='lng' placeholder='Decimal Longitude' value='{0}'></td>".format(lng),
        "</tr>",
        "<tr>",
          "<td align='right'>Sunrise Offset:</td>",
          "<td><input type='text' name='sunrise_offset' placeholder='0' value='{0}'></td>".format(sunrise_offset),
        "</tr>",
        "<tr>",
          "<td align='right'>Sunset Offset:</td>",
          "<td><input type='text' name='sunset_offset' placeholder='0' value='{0}'></td>".format(sunset_offset),
        "</tr>",
        "<tr>",
          "<td align='right'>Pushover App Token:</td>",
          "<td><input type='text' name='app_token' placeholder='Pushover App Token' value='{0}'></td>".format(app_token),
        "</tr>",
        "<tr>",
          "<td align='right'>Pushover Group Key:</td>",
          "<td><input type='text' name='group_key' placeholder='Pushover Group or user key' value='{0}'></td><br>".format(group_key),
        "</tr>",
        "<tr>",
          "<td align='right'>Motor Min Frequency:</td>",
          "<td><input type='text' name='motor_min' placeholder='Minimum motor frequency' value='{0}'></td><br>".format(motor_min),
        "</tr>",
        "<tr>",
          "<td align='right'>Motor Max Frequency:</td>",
          "<td><input type='text' name='motor_max' placeholder='Maximum motor frequency' value='{0}'></td><br>".format(motor_max),
        "</tr>",
        "<tr>",
          "<td align='right'>Motor ramp steps:</td>",
          "<td><input type='text' name='ramp_steps' placeholder='Steps increment to ramp acceleration' value='{0}'></td><br>".format(ramp_steps),
        "</tr>",
        "<tr>",
          "<td align='right'>Motor ramp time:</td>",
          "<td><input type='text' name='ramp_time' placeholder='Time in ms between ramp increments' value='{0}'></td><br>".format(ramp_time),
        "</tr>",
        "<tr>",
          "<td><button type='submit' name='save' value='save'>Save Configuration</button></td>",
          "<td><button type='submit' name='reset' value='reset'>Reset Device</button></td>",
        "</tr>",
        "<tr>",
          "<td colspan='2'><h3>{0}</h3></td>".format(message),
        "</tr>",
      "</table>",
    "</center></form>",
    "</html>",
    ]
    
    html_string = "\n".join(html_list)
    return html_string
    

    

  def update_config(self):
    _thread.start_new_thread(self.update_reset_monitor,())
    from microdot import Microdot,redirect,send_file,Response
    app = Microdot()

    ap_ssid = 'ChickenCoup-ConfigMode'
    ap_password = '123456789'
    
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=ap_ssid, password=ap_password)

    @app.route("/", methods=['GET','POST'])    
    def index(request):
      #form_cookie= None
      #message_cookie = None
      if request.method == "GET":
        #print(dir(request))
        #print(request.form)
        #print(request.headers)
        return Response(body=self.build_html_form(),headers={"Content-Type": "text/html"})
      elif request.method == "POST":
        if request.form.get('save',None):
          ## they clicked save config. write the dict to flash as a json file...
          print(request.form)
          new_config = {
            "wifi": {
              "ssid": request.form['ssid'],
              "passphrase": request.form['passphrase']
             },
             "location": {
               "lat": request.form['lat'],
               "lng": request.form['lng']
             },
             "time": {
               "sunrise_offset": request.form['sunrise_offset'],
               "sunset_offset": request.form['sunset_offset']
             },
             "pushover": {
               "app_token": request.form['app_token'],
               "group_key": request.form['group_key']
             },
             "motor_tuning": {
               "motor_min": request.form['motor_min'],
               "motor_max": request.form['motor_max'],
               "ramp_steps": request.form['ramp_steps'],
               "ramp_time": request.form['ramp_time'],
             }
          }

          print(new_config)
          with open("config.json",'w',encoding = 'utf-8') as f:
            print("Saving configuration to config.json...")
            f.write(json.dumps(new_config))
            print("Done!")


          self.json_config = new_config
          return Response(body=self.build_html_form(message="Updated Configuration!"),headers={"Content-Type": "text/html"})


        elif request.form.get('reset',None):
          ## They clicked on reset! Reset the device!
          self.update_reset_scheduled = True
          return send_file('reset.html')
          
    app.run(debug=True)


  def update_reset_monitor(self):
    ## only really used by the update config function
    ## this background thread will reset the device 5s after
    ## a variable flag is detected. This allows an http response
    ## to be sent, avoiding errors in the browser...
    while True:
      if getattr(self,'update_reset_scheduled',None):
        sleep(5)
        reset()


  def setup_logger(self):
    logging.basicConfig(level=logging.INFO)
    self.log = logging.getLogger("ChickenDoor")
 
  def blink(self):
    while True:
      if self.blink_freq:
        self.led.value(1)
        self.activity_led.value(1)
        #print("led ON")
        sleep(self.blink_freq)
        self.led.value(0)
        self.activity_led.value(0)
        #print("led OFF")
        sleep(self.blink_freq)
      else:
        self.led.value(0)
        self.activity_led.value(0)


  def mode_callback(self,pin):
    reset()
        

  def time_monitor(self):
    while True:
      if self.next_operation_time:
        time_till_operation = ((self.next_operation_time - utime.time()))
        door_status = self.check_limits()
        if utime.time() > self.next_operation_time:
          if self.next_operation == "open":
            if door_status['actual'] != "open":
              self.open()
            #else:
            #  self.standby(duration=time_till_operation)
          elif self.next_operation == "close":
            if door_status['actual'] != "closed":
              self.close()
            #else:
            #  self.standby(duration=time_till_operation)
          else:
            # This shouldnt happen. its here for completeness
            print("errmagherd, something is wrong")

          ## Update the next_operation_time using this offset...
          self.calculate_next_operation(offset=1)

          time_till_operation = ((self.next_operation_time - utime.time()))

          #hours,minutes_remainder,seconds_remainder = self.convert_time(time_till_operation)
          sleep(1)

          while utime.time() < (self.pending_operation_time + self.operation_timeout):
            if self.pending_operation:
              sleep(5)

          self.log.info("Its {0} until the next operation".format(self.convert_time(time_till_operation)))
          self.standby(duration=time_till_operation)

        else:
          #hours,minutes_remainder,seconds_remainder = self.convert_time(time_till_operation)
          #print("Its {0}:{1}:{2} until the next operation".format(hours,minutes_remainder,seconds_remainder))
          self.log.info("Its {0} until the next operation".format(self.convert_time(time_till_operation)))
          if self.next_operation == "open":
            if door_status['actual'] != "closed":
              # The door should be shut right now! Close it!
              self.close()
          elif self.next_operation == "close":
            if door_status['actual'] != "open":
              # The door should be open right now! Open it!
              self.open()
          
          #hours,minutes_remainder,seconds_remainder = self.convert_time(time_till_operation)
          #print("Its {0}:{1}:{2} until the next operation".format(hours,minutes_remainder,seconds_remainder))
          #print("Its {0} until the next operation".format(self.convert_time(time_till_operation)))
          while utime.time() < (self.pending_operation_time + self.operation_timeout):
            if self.pending_operation:
              sleep(5)

          self.standby(duration=time_till_operation)

        sleep(60)
      else:
        print("waiting for sunset/sunrise data...")
        sleep(1)

 
  def convert_time(self,seconds):
    minutes = int(seconds / 60)
    seconds_remainder = int(seconds % 60)
    hours = int(minutes / 60)
    minutes_remainder = int(minutes % 60)
  
    time_str = "{:0>2d}:{:0>2d}:{:0>2d}".format(hours,minutes_remainder,seconds_remainder)

    return time_str

     
  def calculate_next_operation(self,offset=0):
      #self.sunrise_dict['current'] = utime.time()

      #print(self.sunrise_dict)

      sorted_dates = sorted(self.sunrise_dict.values())
      next_operation_index = sorted_dates.index(self.sunrise_dict['current'])+ 1 + offset
      next_operation_time = sorted_dates[next_operation_index]

      for name,datestamp in self.sunrise_dict.items():
        if datestamp == next_operation_time:
          print(name)
          if name.endswith("sunrise"):
            ## Open the door 2h after sunrise. allowing time for the chickens to lay eggs and stuff.
            self.next_operation = "open"
            self.next_operation_time = next_operation_time
            #self.next_operation_time = (next_operation_time + self.sunrise_offset)
          elif name.endswith("sunset"):
            ## close the door 10m before sunset
            self.next_operation = "close"
            self.next_operation_time = next_operation_time
            #self.next_operation_time = (next_operation_time + self.sunset_offset)

  def get_sunrise_sunset(self):
    sun = Sun(self.lat,self.lng,0)

    sunrise_sunset_dict = {}
    current_time = utime.time()
    today = utime.localtime(current_time)[:3]
    tomorrow = utime.localtime(current_time + 86400)[:3]
    yesterday = utime.localtime(current_time - 86400)[:3]
    days = {
      'yesterday': yesterday,
      'today': today,
      'tomorrow': tomorrow
    }

    for day in days:
      sunrise = sun.get_sunrise_time(days[day])
      sunset = sun.get_sunset_time(days[day])
 

      ## Adding the sunrise and sunset offsets to these values to make the logic more sane.
      sunrise_sunset_dict['{0}_sunrise'.format(day)] = (utime.mktime(sunrise+(0,0,0)) + self.sunrise_offset)
      sunrise_sunset_dict['{0}_sunset'.format(day)] = (utime.mktime(sunset+(0,0,0)) + self.sunset_offset)

    sunrise_sunset_dict['current'] = current_time

    self.sunrise_dict = sunrise_sunset_dict
    self.calculate_next_operation()



  def load_config(self):
    try:
      with open("config.json","r") as w:
        json_string = w.read()
        self.json_config = json.loads(json_string)
        self.ssid = self.json_config['wifi']['ssid']
        self.passphrase = self.json_config['wifi']['passphrase']
        self.lat = float(self.json_config['location']['lat'])
        self.lng = float(self.json_config['location']['lng'])
        self.sunrise_offset = int(self.json_config['time']['sunrise_offset'])
        self.sunset_offset = int(self.json_config['time']['sunset_offset'])
        self.app_token = self.json_config['pushover']['app_token']
        self.group_key = self.json_config['pushover']['group_key']
        self.close_attempts = 0
        self.is_stepper = True
        self.invert_dir = False
        self.limit_sense_time = None
        self.input_sense_time = None
        self.input_sense_count = 0
        self.limit_sense_count = 0
        self.pending_operation = False
        self.pending_operation_time = 0
        self.operation_timeout = 120
        self.notification_sent = False
        self.motor_min = int(self.json_config['motor_tuning']['motor_min'])
        self.motor_max = int(self.json_config['motor_tuning']['motor_max'])
        self.motor_ramp_time = int(self.json_config['motor_tuning']['ramp_time'])
        self.motor_ramp_steps = int(self.json_config['motor_tuning']['ramp_steps'])
       

        #self.is_stepper = False
        return True
    except:
      # Config file doesnt exist! Start in AP Mode for initial configuration...
      #self.update_config(ap_mode=True)
      #self.json_config = None
      return False
      


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
    connect_wait = 1
    while connect_wait <= 30:
      #wait for the connection fully activate
      print("Waiting for connection to activate...")
      if  self.sta_if.isconnected():
        break
      sleep(1)
      connect_wait += 1
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

  def close(self,notify=True,duration=None,attempt=0):
    gc.collect()
    if self.is_stepper:
      #do stepper things...
      if duration:
        close_time = utime.time() + duration
      else:
        open_time = None
      #sleep(0.5)
      with open("state.txt",'w',encoding = 'utf-8') as f:
        f.write("closed")

      if self.invert_dir:
        self.dir.value(not self.close_dir)
      else:
        self.dir.value(self.close_dir)

      if self.close_limit.value() == 1:
        self.log.info("Door is already closed!")
        return
      else:
        self.operation = "close"
        self.enable_motor()


  def open(self,notify=True,duration=None):
    gc.collect()

    if self.is_stepper:
      #do stepper things...
      if duration:
        close_time = utime.time() + duration
      else:
        open_time = None
      #sleep(0.5)
      with open("state.txt",'w',encoding = 'utf-8') as f:
        f.write("open")
      ## maybe set some direction pins here????

      if self.invert_dir:
        self.dir.value(not self.open_dir)
      else:
        self.dir.value(self.open_dir)

      if self.open_limit.value() == 1:
        self.log.info("Door is already open!")
        return
      else:
        self.operation = "open"
        self.enable_motor()



  def send(self,token,user,message,priority=0):
    attempts = 0
    while attempts < 5:
    #while True:
      gc.collect()
      try:
        pushover_url = "https://api.pushover.net/1/messages.json"
        headers = {'Content-Type': 'application/json'}
        json_data = json.dumps({'token': token,"user": user,"message": message, "priority": priority})
        response = urequests.post(url=pushover_url,headers=headers,data=json_data)
        print(response.status_code)
        return response
        break
      except:
        print(micropython.mem_info())
        attempts += 1
        sleep(5)


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
    One of the limit switches should be open,and one closed. Planning on using normally close
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

  def standby(self,duration=None):
    #self.slp.init(Pin.PULL_HOLD)
    #duration should be in seconds    

    #level parameter can be: esp32.WAKEUP_ANY_HIGH or esp32.WAKEUP_ALL_LOW
    esp32.wake_on_ext0(pin = self.manual_open, level = esp32.WAKEUP_ALL_LOW)
    
    # Couldnt get ext1 with two wakeup switches to work. leave it here for knowledge...
    esp32.wake_on_ext1(pins=[self.manual_close], level=esp32.WAKEUP_ALL_LOW)
    #esp32.wake_on_ext1(pins = [self.manual_open, self.manual_close], level = esp32.WAKEUP_ALL_LOW)
    
    
    ###  1000 * 60 * 10 = 10m in milliseconds
    if duration:
      print('Going to sleep in 30s in case there are any pending threads...')
      sleep(30)
      print('Going to sleep now...')
      sleepytime =  duration * 1000
      deepsleep(sleepytime)
    else:
      # no duration specified, go into deepsleep indefinitely
      print('Going to sleep now...')
      deepsleep()


door = ChickenDoor()
door.blink_freq = 0.5

while True:
  # Waiting for things to happen
  gc.collect()
  gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
  sleep(1)

