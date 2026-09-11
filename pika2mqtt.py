#!/usr/bin/env python3
#
#
import requests
import time
import threading
import argparse
import os
import logging
import signal
import sys

from pika_transport import (
  SshTunnelConfig,
  SshTunnelSupervisor,
  TransportConfigurationError,
)

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
  GRIDTIE = 8

  def __init__(self, rcpn, entry, power = None):
    if power is None:
      self._timestamp = None
      self._rcpn = rcpn
      self.noupdate = 0
      self.update(entry)
    else:
      self._timestamp = int(time.time())
      self._rcpn = 'DEADCAFEBEEF'
      self.noupdate = 0
      self.type = PikaDevice.GRIDTIE
      self.serial = self._rcpn
      self.name = 'grid'
      self.state = 0
      self.output = max(0,power)
      self.input = abs(min(0, power))
      self.power = power
      self.charge = 0
      self.modid = -1
      self.lastupdate = self._timestamp

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
    types = ['Unknown', 'Wind', 'Inverter', 'Solar', 'Weatherstation', 'Battery', 'Load', 'Beacon', 'Gridtie']
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
  IGNORE_TYPES = [PikaDevice.BEACON, PikaDevice.UNKNOWN, PikaDevice.WEATHERSTATION]

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
            device = PikaDevice(i['rcpn'], data)
            if device.type not in Pika.IGNORE_TYPES:
              self.devices.append(device)

  def add_gridtie(self, power):
    self.devices.append(PikaDevice(None, None, power=power))

class PikaMonitor(threading.Thread):
  def __init__(self, base_url, prefix, ignoreSerials=None, transport=None):
    threading.Thread.__init__(self)
    self.daemon = True
    self.url = base_url
    self.mqtt = None
    self.prefix = prefix
    self.ignore = ignoreSerials or []
    self.transport = transport
    self.topics = {}
    self.stop_event = threading.Event()

    if self.prefix[-1] != '/':
      self.prefix += '/'

  def start(self, mqtt):
    self.mqtt = mqtt
    threading.Thread.start(self)

  def stop(self):
    self.stop_event.set()

  def _request(self, url):
    try:
      result = requests.get(url, timeout=5)
      if self.transport:
        self.transport.report_success()
      return result
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
      if self.transport:
        self.transport.report_transport_failure(error)
      raise

  def load_devices(self):
    try:
      url = self.url + '/devices'
      result = self._request(url)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
      logging.warning('Failed to connect to devices endpoint')
      return None
    except:
      logging.exception('Failed to get devices')
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
      result = self._request(self.url + '/device/%d/model/inverter_status' % id)
      if result is None or result.status_code != 200:
        logging.error(f'Failed to obtain gridtie information.')
        if result:
          logging.error(f'Result is: {result.status_code}')
      else:
        tie = result.json()
        if 'fixed' in tie and 'CTPow' in tie['fixed']:
          return tie['fixed']['CTPow']
        else:
          logging.error(f'Failed to obtain gridtie information.')
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
      logging.warning('Failed to connect to inverter status endpoint')
    except:
      logging.exception('Failed to obtain gridtie information')
    logging.warning('No gridtie information found')
    return None

  def publish(self, topic, key, value):
    topic = self.prefix + topic
    self.mqtt.publish(topic + '/' + key, value)

  def run(self):
    last_update = {} # Track the last update time for each device
    last_solar = -1
    power = None
    logging.info('Starting the monitor')
    while not self.stop_event.is_set():
      if self.transport and not self.transport.wait_available(timeout=1):
        continue
      total_solar = 0
      # First, fetch the devices
      pika = self.load_devices()
      if pika is None:
        logging.warning('No devices found; waiting for installer transport')
        self.stop_event.wait(1)
        continue

      # Next, fetch the inverter id so we can get the gridtie information
      inv = pika.find(type = PikaDevice.INVERTER)
      if inv:
        power = self.load_gridtie(inv.modid)
        if power != None:
          pika.add_gridtie(power)
      else:
        logging.warning('No inverter found in device response')
        self.stop_event.wait(1)
        continue

      if not pika and not power:
        logging.warning('No devices found in installer response')
        self.publish('connected', 'state', 0)
        self.stop_event.wait(1)
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
          kWh = (entry.power * (entry.lastupdate - last_update[entry.serial])) / 3600

        logging.debug('%1s %10d | %8d | %2.1f | %s | %20s | %10d | %s' % ('' if skip else '*', entry.lastupdate, entry.power, entry.charge, entry.serial, f'{entry.name} [{entry.getTypeName()}]', last_update[entry.serial], ('%.5f' % kWh) if kWh != None else 'Not available'))
        last_update[entry.serial] = entry.lastupdate
        if skip:
          continue

        topic = ('%s_%s' % (entry.getTypeName(), entry.serial)).lower()
        if entry.type == PikaDevice.SOLAR:
          total_solar += entry.output

        if entry.hasPower():
          self.publish(topic, 'output', entry.output)
          self.publish(topic, 'input', entry.input)
          if kWh != None:
            self.publish(topic, 'input_kwh', abs(kWh) if kWh < 0 else 0)
            self.publish(topic, 'output_kwh', kWh if kWh > 0 else 0)
        self.publish(topic, 'power', entry.power)
        if kWh != None:
          self.publish(topic, 'power_kwh', kWh)

        if entry.type == PikaDevice.BATTERY:
          self.publish(topic, 'charge', int(entry.charge*10))

      # Do an estimation on the amount of kWh produced by the solar array
      kWh = (total_solar) / 3600

      # Publish a total solar as well (if it changed)
      if total_solar != last_solar:
        last_solar = total_solar
        self.publish('solar_total', 'power', total_solar)
        self.publish('solar_total', 'power_kwh', kWh)
        self.publish('solar_total', 'output', total_solar)
        self.publish('solar_total', 'output_kwh', kWh if kWh > 0 else 0)

      logging.debug(f'Total solar: {total_solar}W ({kWh}kWh)')

      self.stop_event.wait(1)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def build_parser():
  parser = argparse.ArgumentParser(description="Pika-2-MQTT - Getting that data into your own system", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('hostname', help='IP or FQDN of your pika system')
  parser.add_argument('mqtt', help='MQTT Broker to publish topics')
  parser.add_argument('--user', help='MQTT Broker user')
  parser.add_argument('--password', help='MQTT Broker password')
  parser.add_argument('basetopic', help='What base topic to use, is prefixed to /<type>_<serial>/x where x is one of watt or state')
  parser.add_argument('--idrsa', default='/key/id_rsa', help='Path to the root SSH key for the Pika system')
  parser.add_argument('--ssh-host-fingerprint', default=os.getenv('SSH_HOST_FINGERPRINT'), help='Expected SHA-256 SSH host-key fingerprint')
  parser.add_argument('--ssh-port', type=int, default=int(os.getenv('SSH_PORT', '22')), help='Inverter SSH port')
  parser.add_argument('--ssh-local-port', type=int, default=int(os.getenv('SSH_LOCAL_PORT', '18080')), help='Loopback port used by the SSH tunnel')
  parser.add_argument('ignore', nargs='*', help='Serial of devices to ignore')
  parser.add_argument('--debug', action='store_true', help='Enable debug logging')
  return parser


def main(argv=None):
  try:
    import paho.mqtt.client as mqtt
  except ImportError:
    logging.critical('Missing runtime dependency: paho-mqtt')
    return 2

  parser = build_parser()
  cmdline = parser.parse_args(argv)

  if cmdline.debug:
    logging.getLogger().setLevel(logging.DEBUG)

  tunnel = SshTunnelSupervisor(SshTunnelConfig(
    hostname=cmdline.hostname,
    private_key=cmdline.idrsa,
    host_fingerprint=cmdline.ssh_host_fingerprint,
    ssh_port=cmdline.ssh_port,
    local_port=cmdline.ssh_local_port,
  ))
  try:
    tunnel.start()
  except TransportConfigurationError as error:
    logging.critical('%s', error)
    return 2

  client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
  if cmdline.user and cmdline.password:
    client.username_pw_set(cmdline.user, cmdline.password)
  else:
    logging.warning('Not using MQTT authentication')

  monitor = PikaMonitor(tunnel.base_url, cmdline.basetopic, cmdline.ignore, transport=tunnel)

  def shutdown(signum=None, frame=None):
    if signum is not None:
      logging.info('Received signal %d, shutting down', signum)
    monitor.stop()
    client.disconnect()

  previous_handlers = {}
  if threading.current_thread() is threading.main_thread():
    for signum in (signal.SIGTERM, signal.SIGINT):
      previous_handlers[signum] = signal.signal(signum, shutdown)

  def watch_fatal_transport():
    if tunnel.wait_fatal():
      logging.critical('SSH tunnel stopped: %s', tunnel.fatal_error)
      client.disconnect()

  fatal_watcher = threading.Thread(target=watch_fatal_transport, daemon=True)
  fatal_watcher.start()

  try:
    client.connect(cmdline.mqtt, 1883, 60)
    monitor.start(client)
    client.loop_forever()
  finally:
    monitor.stop()
    tunnel.stop()
    for signum, previous in previous_handlers.items():
      signal.signal(signum, previous)

  return 1 if tunnel.fatal_error else 0


if __name__ == '__main__':
  sys.exit(main())
