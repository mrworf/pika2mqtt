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

  def update(self, devicelist):
    found = []
    for device in devicelist:
      for i in range(0, len(self.devices)):
        if self.devices[i].serial == device.serial:
          self.devices[i] = device
          found.append(device)
          break
    for device in devicelist:
      if not device in found:
        self.devices.append(device)

class PikaMonitor(threading.Thread):
  def __init__(self, profile, prefix):
    threading.Thread.__init__(self)
    self.daemon = True
    self.profile = profile
    self.mqtt = None
    self.prefix = prefix
    if self.prefix[-1] != '/':
      self.prefix += '/'

  def start(self, mqtt):
    self.mqtt = mqtt
    threading.Thread.start(self)

  def run(self):
    pika = Pika()
    topics = {}
    while True:
      try:
        result = requests.get(self.profile)
      except requests.exceptions.ConnectionError:
        print('Connection error')
        time.sleep(1)
        continue

      if result is None or result.status_code != 200:
        print('Failed to obtain result from URL')
        time.sleep(REFRESH)
        continue

      j = result.json()
      print('\x1b[H\x1b[2JSystem status:')
      print('%-8s   %20s . %-8s . %-12s . Device and power' % ('flags', '', 'watts', 'serial'))
      for i in range(0, len(j['dvcs'])):
        entry = PikaDevice(j['dvcs'][i])
        pika.update([entry])

      for entry in pika.devices:
        print('0x%08x %-20.20s | %8d | %s | %s [%s] | %.1f' % (entry.state, entry.getStateDefinition().description, entry.output, entry.serial, entry.name, entry.getTypeName(), entry.charge))
        topic = (self.prefix + '%s_%s' % (entry.getTypeName(), entry.serial)).lower()
        output = entry.output
        input = entry.input
        state = entry.state
        charge = entry.charge

        if topic not in topics or topics[topic]['state'] != state:
          print('Publishing new state (%x) for %s' % (state, topic))
          self.mqtt.publish(topic + '/state', state)

        if entry.hasPower():
          if topic not in topics or topics[topic]['output'] != output:
            print('Publishing new output (%d) for %s' % (output, topic))
            self.mqtt.publish(topic + '/output', output)
          if topic not in topics or topics[topic]['input'] != input:
            print('Publishing new input (%d) for %s' % (input, topic))
            self.mqtt.publish(topic + '/input', input)

        if entry.type == PikaDevice.BATTERY and (topic not in topics or topics[topic]['charge'] != charge):
          print('Publishing new charge (%.1f) for %s' % (charge, topic))
          self.mqtt.publish(topic + '/charge', int(charge * 10))
        topics[topic] = {'input': input, 'output': output, 'state':state, 'charge':charge}

      time.sleep(REFRESH)

parser = argparse.ArgumentParser(description="Pika-2-MQTT - Getting that data into your own system", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--logfile', metavar="FILE", help="Log to file instead of stdout")
parser.add_argument('url', help='Public PIKA url (for example https://profiles.pika-energy.com/users/0123456789)')
parser.add_argument('mqtt', help='MQTT Broker to publish topics')
parser.add_argument('basetopic', help='What base topic to use, is prefixed to /<type>_<serial>/x where x is one of watt or state')
cmdline = parser.parse_args()

m = re.match('https://profiles.pika-energy.com/users/([0-9]+)', cmdline.url)
if m is None:
  print('URL "%s" is not valid' % cmdline.url)
  sys.exit(255)

client = mqtt.Client()
#client.on_connect = on_connect
#client.on_message = on_message
client.connect(cmdline.mqtt, 1883, 60)

monitor = PikaMonitor('https://profiles.pika-energy.com/%s.json' % m.group(1), cmdline.basetopic)
monitor.start(client)
client.loop_forever()
