# Pika To MQTT

This handy little tool will allow you to scrape the Pika website or the Pika inverter itself (if you run it in installer mode) and publish it via MQTT protocol.

It was created since I didn't particularly care for their website and because I do this with all other data sources around my house, and it would thus allow me to coalesce data points to create new features (like, if we generate enough power and the temperature is too high, start the AC even if we're not home, etc).

## Usage

It's really simple, it requires the URL to your pika profile. Unfortunately, because I'm lazy, you will have to make your solar system public so no login is required to access the details. Once this has been done, you "simply" run `pika2mqtt.py` with the following parameters:

### With the public profile information

- url to your profile (for example, https://profiles.pika-energy.com/users/0123456789)
- MQTT broker (for example, mqtt.local)
- A base for all topics to be published, I use `house/energy`
- Zero or more serials (all uppercase) which should be ignored

A complete command line would look like this (using above information)

```
./pika2mqtt.py https://profiles.pika-energy.com/users/0123456789 mqtt.local house/energy
```

### With the Pika inverter in installer mode

- url to your inverter, simply the IP/DNS name of it (for example, http://my-inverter.domain.net)
- MQTT broker (for example, mqtt.local)
- A base for all topics to be published, I use `house/energy`
- Zero or more serials (all uppercase) which should be ignored

Once started, the tool and begin polling the JSON endpoints every minute (that's as often as they refresh the data on their website, so don't bother hitting it harder).

As it runs, it will print out whenever it changes the values. It will not post duplicate values.

For all power generating/charging units, it will produce a topic ending with `output` for when it's producing power and `input` for when consuming. Typically only the battery will consume power (well, the inverter sometimes does it too if the sun is down and your system is grid tied).

All devices publish a `state` which is the raw state of the unit (see source for how to map that if you're interested). A bettery will also publish a `charge` topic, which indicates the charge of your battery. It's going to be a multiple of 10 since I want to avoid floats but still keep 1 fraction's precision (same as pika). So 945 is 94.5%

The topic also contains the device type and the serial number, so a solar panel with the id `00010003BEEF` will publish the following topics:

```
house/energy/solar_00010003BEEF/output
```

And a battery will look like this

```
house/energy/battery_00010003BEEF/input
house/energy/battery_00010003BEEF/output
house/energy/battery_00010003BEEF/charge
```

There's also a couple of extra topics. The next one is simply an additon of all solar panel output

```
house/energy/total_solar/output
```

If you're using installer mode, it will also provide
```
house/energy/grid/output
house/energy/grid/input
house/energy/house/input
```

This uses the Current transformers (CT) which are clamped around the feed to your main panel and will show how much you consume from the grid (output) or sell to the grid (input). Likewise, house topic implies how much power your household is consuming.

These three topics will *only* be available if you run in installer mode and have CT installed properly (some installers will sometimes install them on the feed between the main panel and the inverter, which obviously will be a bit misleading).

Now, when you combine this tool with [telegraf](https://www.influxdata.com/time-series-platform/telegraf/ "telegraf"), [influxDB](https://www.influxdata.com/products/influxdb-overview/ "influxDB") and [grafana](https://grafana.com/ "grafana"), you get nice looking items such as

![grafana scrennshot](images/grafana.png "Grafana screenshot")

## Docker image

To simplify things, you can also run this as a docker image. The arguments above are provided via environment variables URL, MQTT and BASETOPIC. You need to run it with a terminal (ie, `-t`) or it will not work

The project is published on https://hub.docker.com/r/mrworf/pika2mqtt
