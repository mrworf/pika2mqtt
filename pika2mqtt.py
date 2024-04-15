#!/usr/bin/env python3
#
#
import requests
import time
import threading
import argparse
from datetime import datetime, timezone
import subprocess
import os
import paho.mqtt.client as mqtt
import logging
import time
import os

REFRESH=15

class PikaState:
  class Status:
    NEUTRAL = 0
    GOOD = 1
    BAD = -1

    def __init__(self, code, description, severity = GOOD):
      self.code = code
      self.description = description
      self.severity = severity

  DEFINITIONS = {
    'UNDEFINED' : Status(0x00000000, "Undefined", Status.NEUTRAL),
    'DISABLED' : Status(0x00000010, "Disbled", Status.NEUTRAL),
    'INITIALIZING' : Status(0x00000100, "Initializing", Status.NEUTRAL),
    'POWERING_UP' : Status(0x00000110, "Powering up"),
    'REBUS_CONNECTING' : Status(0x00000120, "Connecting to bus"),
    'REBUS_DISCONNECTING' : Status(0x00000130, "Disconnecting from bus", Status.NEUTRAL),
    'REBUS_TESTING' : Status(0x00000140, "Testing bus", Status.NEUTRAL),
    'REBUS_LOW_VOLTAGE' : Status(0x00000200, "Low bus voltage", Status.NEUTRAL),
    'STANDBY' : Status(0x00000300, "Standby", Status.NEUTRAL),
    'WAITING' : Status(0x00000310, "Waiting", Status.NEUTRAL),
    'GRID_CONNECTING' : Status(0x00000800, "Connecting grid"),
    'GRID_DISCONNECTING' : Status(0x00000810, "Disconnecting grid"),
    'GRID_CONNECTED' : Status(0x00000820, "Grid connected"),
    'ISLANDED' : Status(0x00000830, "Islanded"),
    'INPUT_LOW_VOLTAGE' : Status(0x00001000, "Low input voltage", Status.NEUTRAL),
    'INPUT_TESTING' : Status(0x00001010, "Testing input", Status.NEUTRAL),
    'RUNNING' : Status(0x00002000, "Running"),
    'POWER_MAKING' : Status(0x00002010, "Making power"),
    'POWER_LIMITING' : Status(0x00002020, "Limiting power"),
    'WIND_LOW' : Status(0x00003000, "Low wind"),
    'SUN_LOW' : Status(0x00003100, "Low sun"),
    'BATTERY_CHARGING' : Status(0x00006000, "Charging battery"),
    'BATTERY_CHARGING_2' : Status(0x00006020, "Charging battery"),
    'BATTERY_REGULATING' : Status(0x00006010, "Regulating battery"),
    'BATTERY_DISCHARGING' : Status(0x00006100, "Discharging battery"),
    'BATTERY_CELL_IMBAL' : Status(0x00006300, "Cell imbalance", Status.BAD),
    'ERROR' : Status(0x00007000, "Error", Status.BAD),
    'INPUT_OVER_VOLTAGE' : Status(0x00007010, "Input over-voltage", Status.BAD),
    'OUTPUT_OVER_VOLTAGE' : Status(0x00007020, "Output over-voltage", Status.BAD),
    'INPUT_OVER_CURRENT' : Status(0x00007030, "Input over-current", Status.BAD),
    'OUTPUT_OVER_CURRENT' : Status(0x00007040, "Output over-current", Status.BAD),
    'OVERHEATING' : Status(0x00007100, "Overheating", Status.BAD),
    'OFFLINE' : Status(0x00008000, "Offline", Status.NEUTRAL)
  }



class PikaDevice:
  WIND = 1
  INVERTER = 2
  SOLAR = 3
  WEATHERSTATION = 4
  BATTERY = 5
  LOAD = 6
  BEACON = 7
  UNKNOWN = 0

  def __init__(self, rcpn, entry):
    self._timestamp = None
    self._rcpn = rcpn
    self.noupdate = 0
    self.update(entry)

  def update(self, data):
    # Locate the device info we need
    entry = None
    for k in data:
      for i in data[k]:
        if i['rcpn'] == self._rcpn and i['modID'] is not None and i['lastheard'] is not None:
          entry = i
          break
      if entry: break

    if not entry:
      print('Error: Cannot find RCPN item %s' % self._rcpn)
      return

    self.type = self.determineType(entry['rcpn'])
    self.serial = entry['rcpn']
    self.name = entry['type']
    self.state = 0
    self.output = max(0,entry['power'])
    self.input = abs(min(0, entry['power']))
    self.power = entry['power'] # Unalterated value
    self.charge = entry.get('soc', 0)
    self.modid = entry['modID']
    self.lastupdate = int(time.time() - entry['lastheard'])

  def getStateDefinition(self):
    return PikaState.DEFINITIONS['UNDEFINED']

  def getTypeName(self):
    types = ['Unknown', 'Wind', 'Inverter', 'Solar', 'Weatherstation', 'Battery', 'Load', 'Beacon']
    return types[max(0, min(len(types)-1, self.type))]

  def hasPower(self):
    return self.type not in [PikaDevice.BEACON, PikaDevice.UNKNOWN, PikaDevice.WEATHERSTATION]

  def determineType(self, serial):
    t = serial[4:8]
    if t == '0001': return PikaDevice.WIND
    if t == '0002' or t == '0007': return PikaDevice.INVERTER
    if t == '0003': return PikaDevice.SOLAR
    if t == '0004': return PikaDevice.WEATHERSTATION
    if t == '0005' or t == '0008': return PikaDevice.BATTERY
    if t == '0006': return PikaDevice.LOAD
    if t == '0012': return PikaDevice.BEACON
    return PikaDevice.UNKNOWN

class Pika:
  def __init__(self):
    self.devices = []

  def find(self, serial = None, type = None):
    found = None
    for device in self.devices:
      if serial and device.serial == serial:
        found = device
      if type and device.type == type:
        found = device
      if found:
        return found
    return None

  def update(self, data):
    for k in data:
      for i in data[k]:
        if 'rcpn' in i and i['modID'] is not None and i['lastheard'] is not None:
          found = False
          for e in range(0, len(self.devices)):
            if self.devices[e].serial == i['rcpn']:
              found = True
              self.devices[e].update(data)
              break
          if not found:
            self.devices.append(PikaDevice(i['rcpn'], data))

class PikaMonitor(threading.Thread):
  def __init__(self, hostname, prefix, ignoreSerials=None, idrsa=None):
    threading.Thread.__init__(self)
    self.daemon = True
    self.hostname = hostname
    self.url = f'http://{hostname}:8000'
    self.mqtt = None
    self.prefix = prefix
    self.ignore = ignoreSerials
    self.idrsa = idrsa
    self.topics = {}

    if self.prefix[-1] != '/':
      self.prefix += '/'

  def start(self, mqtt):
    self.mqtt = mqtt

    # Ensure that we have the service up first
    self.reconnect()

    threading.Thread.start(self)

  def reconnect(self):
    if not self.idrsa or not os.path.exists(self.idrsa):
      logging.warning('No id_rsa file found, cannot restart the service')
      return
    
    logging.debug('Trying to restart the service')
    command = ['extras/keep_running.sh', self.hostname, self.idrsa]
    try:
      process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,text=True)
      output, error = process.communicate()
      return_code = process.returncode
      logging.debug(f'Command {command} returned {return_code}')
      logging.debug('Output:')
      logging.debug(output)
    except:
      logging.exception('Failed to restart the service')
    time.sleep(5) # Give it a chance

  def load_devices(self):
    try:
      url = self.url + '/devices'
      result = requests.get(url, timeout=0.5)
    except requests.exceptions.ConnectionError:
      logging.exception('Failed to connect to URL')
      return None

    if result is None or result.status_code != 200:
      logging.error(f'Failed to obtain devices.')
      if result:
        logging.error(f'Result is: {result.status_code}')
      return None

    j = result.json()
    # Not very efficient to recreate the object every time, but it's a small list
    pika = Pika()
    pika.update(j)
    return pika

  def load_gridtie(self, id):
    try:
      result = requests.get(self.url + '/device/%d/model/inverter_status' % id)
      if result is None or result.status_code != 200:
        logging.error(f'Failed to obtain gridtie information.')
        if result:
          logging.error(f'Result is: {result.status_code}')
      else:
        tie = result.json()
        if 'fixed' in tie and 'CTPow' in tie['fixed']:
          return tie['fixed']['CTPow']
    except requests.exceptions.ConnectionError:
      logging.exception('Failed to connect to URL')
    except:
      logging.exception('Failed to obtain gridtie information')
    return None

  def publish(self, topic, key, value):
    topic = self.prefix + topic
    self.mqtt.publish(topic + '/' + key, value)

  def run(self):
    gridtie = None # Will always be realtime
    prev_gridtie = None
    last_gridtie = 0

    last_update = {} # Track the last update time for each device

    while True:
      total_solar = 0
      print("\033[2J\033[H")
      # First, fetch the devices
      pika = self.load_devices()

      # Next, fetch the inverter id so we can get the gridtie information
      inv = pika.find(type = PikaDevice.INVERTER)
      if inv:
        gridtie = self.load_gridtie(inv.modid)
        last_gridtie = int(time.time())

      if not pika and not gridtie:
        logging.warning('No devices found, trying to restart the service')
        self.publish('connected', 'state', 0)
        self.reconnect()
        continue
      else:
        self.publish('connected', 'state', 1)

      for entry in pika.devices:
        if entry.serial in self.ignore:
          continue
        skip = True
        if entry.serial not in last_update:
          last_update[entry.serial] = 0
        if entry.lastupdate > last_update[entry.serial]:
          skip = False
        # Calculate the kWh
        kWh = None
        if not skip and last_update[entry.serial] > 0:
          #print('Calc %d - %d = %d' % (entry.lastupdate, last_update[entry.serial], entry.lastupdate - last_update[entry.serial]))
          kWh = (entry.output * (entry.lastupdate - last_update[entry.serial])) / 3600
          if kWh < 0:
            print(f'Negative kWh for {entry.serial} ({kWh}), entry.lastupdate={entry.lastupdate}, last_update={last_update[entry.serial]}')
            sys.exit(1)

        print('%1s %10d | %8d | %2.1f | %s | %20s | %10d | %s' % ('' if skip else '*', entry.lastupdate, entry.output, entry.charge, entry.serial, f'{entry.name} [{entry.getTypeName()}]', last_update[entry.serial], ('%.5f' % kWh) if kWh != None else 'Not available'))
        last_update[entry.serial] = entry.lastupdate
        if skip:
          continue
        topic = ('%s_%s' % (entry.getTypeName(), entry.serial)).lower()
        if entry.type == PikaDevice.SOLAR:
          total_solar += entry.output

        self.publish(topic, 'state', entry.state)

        if entry.hasPower():
          self.publish(topic, 'output', entry.output)
          self.publish(topic, 'input', entry.input)

        if entry.type == PikaDevice.BATTERY:
          self.publish(topic, 'charge', int(entry.charge*10))

      # Publish a total solar output index as well
      self.publish('solar_total', 'output', total_solar)

      kWh = None
      if gridtie:
        self.publish('grid', 'export', abs(min(0, gridtie)))
        self.publish('grid', 'import', max(0, gridtie))
        self.publish('grid', 'power', gridtie)
      elif prev_gridtie:
        self.publish('grid', 'export', 0)
        self.publish('grid', 'import', 0)
        self.publish('grid', 'power', 0)
      prev_gridtie = gridtie
      print('%10d | %8d | %2.1f | %12s | %20s | %10d | %s' % (0, gridtie, 0, '', 'Grid Tie', 0, ''))

      # Calculate the some meta data as well:
      # - Total solar output
      # - Total house usage
      # - Inverter loss

      dev = pika.find(type = PikaDevice.INVERTER)
      if dev:
        # Calculate the power that the house is using
        #self.publish('house', 'input', dev.output - ctpow)
        pass

      time.sleep(1)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

parser = argparse.ArgumentParser(description="Pika-2-MQTT - Getting that data into your own system", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--logfile', metavar="FILE", help="Log to file instead of stdout")
parser.add_argument('hostname', help='IP or FQDN of your pika system')
parser.add_argument('mqtt', help='MQTT Broker to publish topics')
parser.add_argument('basetopic', help='What base topic to use, is prefixed to /<type>_<serial>/x where x is one of watt or state')
parser.add_argument('--idrsa', default='/key/id_rsa', help='Path to the id_rsa file for the PIKA system (for monitoring)')
parser.add_argument('ignore', nargs='*', help='Serial of devices to ignore')
cmdline = parser.parse_args()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(cmdline.mqtt, 1883, 60)

monitor = PikaMonitor(cmdline.hostname, cmdline.basetopic, cmdline.ignore, idrsa=cmdline.idrsa)
monitor.start(client)
client.loop_forever()
