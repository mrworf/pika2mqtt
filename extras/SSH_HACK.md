# The SSH/proxy hack

Since Generac has changed how the system works again, we can no longer just open a hole to the installer REST APIs, since they now actively confirm that the source IP is an approved subnet and if not, redirects you to use the subnet on which the WiFi is running. Obviously, this doesn't work so well.

So what to do? We turn to the "distant 3rd option"

## Giving yourself root access

Again,

> ***DISCLAIMER***
> Following this guide and doing the steps outlined to modify your Pika Energy Inverter is done at your own risk. I take no responsibility if anything goes wrong. Nor do I endorse changing any of the installer settings, since it could have a ***very*** negative impact on your system.
>
> While this works great for me, it's not a guarantee that it will for you. And above all, use backups!

Also, all these instructions assumes you're running on linux.

### Step 1: Make a SD card backup

Use your favorite tool (balena Etcher, dd, etc) to make a backup of your SD card

### Step 2: Adding an SSH key

First, we need to generate a SSH key, which you can easily do by

```
ssh-keygen
```
It will ask you where to save the keypair, make your selection and note it down so you can find it.

Next, we mount partition 7 from the SDcard, which, depending on your system can look a bit different, but for me it meant

```
mkdir -p /mnt/tmp         # Make sure we have the directory
mount /dev/sdb7 /mnt/tmp  # Mount the partition
```

Now that you've mounted it, we will proceed to inject our public key, like so

```
cat /where/ever/the/ssh/key/is/id_rsa.pub >>/mnt/tmp/user/root/.ssh/authorized_keys
```

Time to clean up

```
sync # To make sure it's all committed to SD card
umount /mnt/tmp
```

Done, now plug the card back into the pika raspberry pi and reconnect it

### Step 3: Enable access

This is the trick, we will now login to the system and do two things:

1. Open a port we can use, I chose `8000`
2. Install and run a reverse proxy in python which essentially just bridges port `8000` with `80`

First, open an SSH connection

```
ssh -i /where/ever/the/ssh/key/is/id_rsa root@x.x.x.x
```

x.x.x.x should be the IP of the Raspberry Pi inside the inverter.

Now, this should yield something like this
```
ooooo          .oooooo.   ooo        ooooo 
`888'         d8P'  `Y8b  `88.       .888' 
 888         888           888b     d'888  
 888         888           8 Y88. .P  888  
 888         888           8  `888'   888  
 888       o `88b    ooo   8    Y     888  
o888ooooood8  `Y8bood8P'  o8o        o888o 

lcm_00D1 ~ # 
```

You're in!

### Step 4: Hack the planet!

With our newfound root abilities, let's open our port by running

```
iptables -I INPUT 2 -i eth0 -p tcp --dport 8000 -j ACCEPT
```

and with that done, time for the reverse-proxy (and yes, I know, it's the ugliest code you've ever seen)

Run the following in your terminal
```
cat >>pika_proxy.py <<<EOT
#!/usr/bin/env python3

import http.server
import socketserver
import urllib.request
import shutil

PORT = 8000

class OurHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
                print("Path: " + self.path)
                url = f'http://localhost{self.path}'
                self.send_response(200)
                self.end_headers()

                with urllib.request.urlopen(url) as response:
                        self.wfile.write(response.read())

Handler = OurHandler # http.server.SimpleHTTPRequestHandler

with socketserver.TCPServer(("", PORT), Handler, False) as httpd:
        httpd.allow_reuse_address = True
        httpd.server_bind()
        httpd.server_activate()
        httpd.serve_forever()
EOT
```

This will create a file called `pika_proxy.py` which is a Python3 script which will listen on port `8000` and just cross-connect incoming TCP connections to port `80` on the localhost, which is the REST API (aka installer tool).

Finally, make it executable, like so

```
chmod +x pika_proxy.py
```

### Last step: Running it

This is the easy one, just run this command

```
./pika_proxy.py >/dev/null 2>/dev/null &
```

It starts our server, redirects all output to `/dev/null` and forks it `&`. These two things are important, because we don't want the server to keep the connection to the terminal and we don't want to have a SSH connection open to run this.

At this point, you can logout. Just hit `CTRL-D` and you're out.

# Caveats

This isn't a real reverse proxy, it's a hack, and as such, it's not particularly clever and has little to no understanding of the HTTP protocol. So while the installer page may behave somewhat wonky, the actual API we need will work just fine.

Don't forget to alter the URL you use to call the `pika2mqtt.py` and include the new port in the URL. And yes, if you don't like `8000` you can change it, just edit the python script and the iptables rule.

# Todo

Add the ability to pika2mqtt to detect when server is down and automate the above last step so it's hands off.
