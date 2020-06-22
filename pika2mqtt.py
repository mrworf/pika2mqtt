#!/usr/bin/env python3
#
#
import requests
import time
import threading
import argparse
import re
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

REFRESH=60

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

  def __init__(self, entry):
    self._timestamp = None
    self.update(entry)

  def update(self, entry):
    mapping = {
      'name' : 'n',
      'serial' : 's',
      'type' : 't',
      'start' : 'ti',
      'updated' : 'up',
      'power' : 'p',
      'status': 'st',
      'charge': 'O5'
    }

    self.type = self.determineType(entry[mapping['serial']])
    self.serial = entry[mapping['serial']]
    self.name = entry[mapping['name']]
    self.state = entry[mapping['status']]
    self.output = max(0,entry[mapping['power']])
    self.input = abs(min(0, entry[mapping['power']]))
    self.charge = int(entry[mapping['charge']]) / 10.0

    ts = time.strptime(entry[mapping['updated']], '%Y-%m-%d %H:%M:%S %Z')
    ts = datetime.fromtimestamp(time.mktime(ts)).replace(tzinfo=timezone.utc).astimezone(tz=None)
    if self._timestamp == entry[mapping['updated']]:
      self.noupdate += 1
    else:
      self.noupdate = 0
      self._timestamp = entry[mapping['updated']]
    self.lastUpdate = ts

  def getStateDefinition(self):
    for key in PikaState.DEFINITIONS:
      if PikaState.DEFINITIONS[key].code == self.state:
        return PikaState.DEFINITIONS[key]
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

class PikaInstallerDevice:
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
    self.charge = entry.get('soc', 0)
    self.modid = entry['modID']

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
    if 'dvcs' in data:
      for i in range(0, len(data['dvcs'])):
        entry = data['dvcs'][i]
        serial = entry['s']
        found = False

        for i in range(0, len(self.devices)):
          if self.devices[i].serial == serial:
            found = True
            self.devices[i].update(entry)
            break

        if not found:
          self.devices.append(PikaDevice(entry))
    else:
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
              self.devices.append(PikaInstallerDevice(i['rcpn'], data))

  def isConnected(self):
    # Simply check if all devices have a duplicate of 3 or more
    # since that most likely indicates a lack of change as in
    # the pika system not reporting in.
    connected = False
    for device in self.devices:
      connected |= (device.noupdate < 3)
    return connected

class PikaMonitor(threading.Thread):
  def __init__(self, url, prefix, ignoreSerials=None, installerMode=False):
    threading.Thread.__init__(self)
    self.daemon = True
    self.url = url
    self.mqtt = None
    self.prefix = prefix
    self.ignore = ignoreSerials
    self.topics = {}
    self.installerMode = installerMode

    if self.prefix[-1] != '/':
      self.prefix += '/'

  def publish(self, topic, key, value):
    topic = self.prefix + topic
    if topic not in self.topics or self.topics[topic].get(key, None) != value:
      print('Publishing new %s (%s) for %s' % (key, repr(value), topic))
      self.mqtt.publish(topic + '/' + key, value)
      if topic not in self.topics:
        self.topics[topic] = {}
      self.topics[topic][key] = value

  def start(self, mqtt):
    self.mqtt = mqtt
    threading.Thread.start(self)

  def run(self):
    pika = Pika()
    topics = {}
    lastConnect = False
    while True:
      try:
        url = self.url
        if installer:
          url = self.url + '/devices'
        result = requests.get(url)
      except requests.exceptions.ConnectionError:
        print('Connection error')
        time.sleep(1)
        continue

      if result is None or result.status_code != 200:
        print('Failed to obtain result from URL')
        time.sleep(REFRESH)
        continue

      j = result.json()
      pika.update(j)

      # Next, fetch the status of the inverter so we see the grid power
      inv = None
      if self.installerMode:
        inv = pika.find(type = PikaDevice.INVERTER)
        if inv:
          try:
            result = requests.get(self.url + '/device/%d/model/inverter_status' % inv.modid)
            inv = result.json()
          except requests.exceptions.ConnectionError:
            print('Connection error')
            inv = None

      connected = pika.isConnected()

      if not connected:
        print('WARNING! All information is stale, connection between inverter and pika backend is unavailable\n')
      if 'connected' not in topics or topics['connected'] != connected:
        print('Publishing new state (%d) for connected' % connected)
        self.mqtt.publish(self.prefix + 'connected', 1 if connected else 0)
        topics['connected'] = connected

      total_solar = 0

      for entry in pika.devices:
        if entry.serial in self.ignore:
          continue
        print('%8d | %s | %s [%s] | %.1f' % (entry.output, entry.serial, entry.name, entry.getTypeName(), entry.charge))
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
      if inv:
        self.publish('grid', 'input', abs(min(0, inv['fixed']['CTPow'])))
        self.publish('grid', 'output', max(0, inv['fixed']['CTPow']))

      time.sleep(REFRESH)

parser = argparse.ArgumentParser(description="Pika-2-MQTT - Getting that data into your own system", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--logfile', metavar="FILE", help="Log to file instead of stdout")
parser.add_argument('url', help='Public PIKA url (for example https://profiles.pika-energy.com/users/0123456789)')
parser.add_argument('mqtt', help='MQTT Broker to publish topics')
parser.add_argument('basetopic', help='What base topic to use, is prefixed to /<type>_<serial>/x where x is one of watt or state')
parser.add_argument('ignore', nargs='*', help='Serial of devices to ignore')
cmdline = parser.parse_args()

url = cmdline.url
installer = True
m = re.match('https://profiles.pika-energy.com/users/([0-9]+)', cmdline.url)
if m is None:
  print('URL "%s" is assumed to be a PIKA system in installer mode' % cmdline.url)
else:
  url = 'https://profiles.pika-energy.com/%s.json' % m.group(1)
  installer = False

client = mqtt.Client()
client.connect(cmdline.mqtt, 1883, 60)

monitor = PikaMonitor(url, cmdline.basetopic, cmdline.ignore, installer)
monitor.start(client)
client.loop_forever()
