> ***DISCLAIMER***
> Following this guide and doing the steps outlined to modify your Pika Energy Inverter is done at your own risk. I take no responsibility if anything goes wrong. Nor do I endorse changing any of the installer settings, since it could have a ***very*** negative impact on your system.
>
> While this works great for me, it's not a guarantee that it will for you. And above all, use backups!

# Exploring the new Pika SD card layout
Earlier this year (or late 2021), Pika changed from their old format to using the UpdateFactory, an IoT platform for updating. This meant that the previous hack of changing files on the SD card was no longer working and any change you made would be lost by any updates from Pika. 

This document describes what I learned about the new way as well as how we solve the issue of opening up the installer tool to the LAN, allowing REST API calls to get the data
from the inverter.

# The lay of the land
## Partitions
The SD card is partitioned as follows

| Partition | Filesystem | Description |
| --------- | ---------- | ----------- |
| 1 | Win95 | uBoot Windows Boot partition, holds config.txt 
| 2 | squashfs | Provisioned settings (also referred to as Factory)
| 3 | ext4 | Linux boot partition
| 4 | - | Holds remaining partitions
| 5 | squashfs | System Partition (Copy 1)
| 6 | squashfs | System Partition (Copy 2)
| 7 | ext4 | User data (aka persistent data)

*note* Throughout this doc I'll refer to partitions as p1, p2, etc.

The boot partition holds the following information
- Kernel image
- Rescue Image
- Device Tree

### Observations
It uses standard update system to keep it up to date (make sense, why reinvent the wheel?)

Has two system partitions. Only one is active at any time and provides a means to update one and then swap partition on boot to update the other one. Note that this is speculation on my part but that's what I would have done.

Also has a rescue image should all else fail.

# The boot process
The default init isn't the normal `/bin/init` but `/sbin/init.sh` which does all the heavy lifting of preparing the system. Once it's done, it hands over to `/bin/init` for the regular flow.

## What init.sh does
Sidenote, all logs from their script is saved to `/tmp/boot.log`

### 1. Generate following structure on root 
```
/mnt/ro
/mnt/rw
/mnt/factory
/mnt/overlay
```
### 2. Bind root (/) to `/mnt/ro`
Root in this case will be either p5 or p6

### 3. Mount p2 to a tmp folder and copy into /mnt/factory

### 4. Remove tmp folder

### 5. Check filesystem on p7
Should it have errors, reformat to ext4

If it still fails to mount, ignore, it will just live in ram for this session (ie, no persistence)

### 6. Create folders
```
/mnt/rw/user
/mnt/rw/work
```

### 7. Check for /mnt/factory/opt/pika/RCPn
If not present, assumes system isn't provisioned

### 8. Generate SSID 
Using "REBus_Beacon_" and all characters after "0x" in the `/mnt/factory/opt/pika/RCPn` file

### 10. Get password to use for wifi
Use `/mnt/factory/root/wifi_pass` (ie, `/root/wifi_pass` from p2)

### 11. Alter the hostapd.conf file
Use the previously detected SSID and password to update the config file

### 12. Setup SSH
Make sure it forwards a specific port for the beacon (this would be how they can remote troubleshoot/control the system).

Remote host is `cb.pika-energy.com`

### 13. Copy the monit.cfg
This is the tool which makes sure all services are running as expected. Pretty standard opensource tool.

### 14. Mount p7 (user) to /mnt/rw

### 15. Generate an overlay filesystem
- lower dir (read only) is based on `/mnt/ro` which is stacked with `/mnt/factory`
- upper dir (read/write) is based on `/mnt/rw/user`
- work dir is `/mnt/rw/work`

The resulting filesystem is mounted on `/mnt/overlay` resulting in the following merge:

| Priority | Mount point | Source | Access |
| --- | --- | --- | --- 
| 1 | `/mnt/rw/user` | p7 | read write
| 2 | `/mnt/factory` | p2 | read only
| 3 | `/mnt/ro` | original root | read only

Thus, anything found in a mount point with higher priority is preferred to the lower. Any change to a lower priority file will be stored in priority 1.

### 16. Rewrite fstab
`/mnt/overlay/etc/fstab` is rewritten to exclude original boot drive (since it's squashfs and cannot be edited)

### 17. Copy logfile
Logfile `/tmp/boot.log` is copied over to `/mnt/overlay/var/log/boot.log`

### 18. Remap root
`/mnt/overlay` is now `/` and old `/` is now `/mnt`

Also move `/ro` `/rw` `/proc` `/dev` around

### 19. Create serial symlink since that got lost in all this
Symlink `/dev/ttyAMA0` to `/dev/ttyS0`

### 20. Remove old mount
ie `/mnt/proc` and `/mnt/dev`

### 21. Start regular init
That is, `/bin/init`

## Observations
- Pretty neat
- Lots of references to LTE but most likely just be a PPP0 connection to their network
- When is hostapd started? Probably never unless it's in provisioning mode
- User data would be nice to patch, but runs risk of getting erased, making it hard to persist
- Factory is better, but it's a squashfs filesystem and cannot be changed
- init.sh would be great to tweak, but this is part of the update system and as such a bad idea to change
- SSH seems to be setup to automatically SSH into pika, making it vulnerable to DNS hijacking if we know the key, but no reference to this is visible and also has a known host fingerprint that would be hard to fix

> ***SSH is open to connections and does not block `root` nor does it use public/private key***
> 
> Pika Energy/Generac, this needs to change!

## Where to "insert" ourselves
- We need to open the REST API on Port 80
- `/etc/iptables/rules.v4` (and v6) blocks this
- SSH is open... can we use the `root` account?
- `/etc/login.defs` claims it's using `SHA512` but not really, root password is `md5crypt` apparently

### Two possibly approaches
1. Simply patch user space, but expect it to go belly up if they erase it
2. Decompile factory, make changes and resquash it

### Distant 3rd
Use root password to login and apply patch

Cons:
- Can't break password easily (well, at least not me)
- Not persistent
- Would make the vulnerability worse since we'd be publishing the password

> All of the below is outdated, please look at the `SSH_HACK.md` for how we enable similar functionality these days.

--------------------

# How we fix this
At this time, only the first option mentioned above makes sense and has the least impact. However, we need something which is unlikely to be affected by any updates. At the end of the day, we just need to let port 80 stay open for ethernet.

Essentially, 

```
-A INPUT -i eth0 -p tcp --dport 80 -j DROP
-A OUTPUT -o eth0 -p tcp --sport 80 -j DROP
-A INPUT -i ppp0 -p tcp --dport 80 -j DROP
-A OUTPUT -o ppp0 -p tcp --sport 80 -j DROP
```
should say
```
-A INPUT -i eth0 -p tcp --dport 80 -j ACCEPT
-A OUTPUT -o eth0 -p tcp --sport 80 -j ACCEPT
-A INPUT -i ppp0 -p tcp --dport 80 -j ACCEPT
-A OUTPUT -o ppp0 -p tcp --sport 80 -j ACCEPT
```

One way would be to patch `rules.v4` and `rules.v6` and override them in our user filesystem. But it would be very obvious and this file is likely to be changed at some point.

Other option is to just run additional `iptables` commands to reopen the closed ports, but `/etc/rc.local` is VERY likely to be affected by updates.

What to do? Simple, we just override the `iptables-restore` which is the one command which takes the rules files and apply them. By using `sed` and substituting the `iptables-restore` command with a script, we can change the data coming in. And since `iptables-restore` is just a symlink for `xtables-legacy-multi` that can be called directly and will behave as `iptables-restore` if that's the first parameter it gets (as opposed to get it from the command line).

This is what our `/usr/sbin/iptables-restore` now looks like

```
sed 's/port 80 -j DROP/port 80 -j ACCEPT/' | /usr/sbin/xtables-legacy-multi iptables-restore $@
exit $?
```

The sed command replaces any instance of `port 80 -j DROP` with `port 80 -j ACCEPT` which takes care of what we mentioned above. This is subsequently piped to the `xtables-legacy-multi` in `iptables-restore` mode. We also make sure to send any additional parameters, should they be present (thus `$@`)

Finally, `exit $?` makes sure the script returns the actual returncode of `xtables-legacy-multi` allowing anything relying on this command to know the real result.

# Installing the patch
> This guide assumes you're using Linux, I welcome PRs which adds additional OS support

First, take out your raspberry pi from the inverter (top left corner, yes, it's a RPi3, trust me) and disconnect it from USB and network. Grab your favorite miniSD card reader and connect to your computer. Take the SD card from the RPi3 and insert it into the reader.

At this point, linux should have detected all the partitions. You can confirm this by running `dmesg` and look for a line similar to this one

```
sdb: sdb1 sdb2 sdb3 sdb4 < sdb5 sdb6 sdb7 >
```

> If modifying your SD card makes you nervous, I'd recommend making a clone of it first so you can reverse any changes you've made.

You want to mount the last one, which in my case is `sdb7` (7 should be consistent for everyone but `sdb` may differ if you have more drives). 

```
sudo mkdir -p /mnt/tmp           # Create a temp folder
sudo mount /dev/sdb7 /mnt/tmp # Mount Partiton 7 to /mnt/tmp
```

Next we need to create necessary folders and copy the file into place. The `iptables-restore` mentioned here is in the same folder as this file.

```
sudo mkdir -p /mnt/tmp/user/usr/sbin                   # Creates any missing folders
sudo cp iptables-restore /mnt/tmp/user/usr/sbin/       # Copy the file
sudo chmod 755 /mnt/tmp/user/usr/sbin/iptables-restore # Make sure it's executable
```

Almost there, let's unmount the SD card

```
sudo umount /mnt/tmp
```

Remove the SD card and reinsert it into your Raspberry Pi. 

> BE CAREFUL TO INSERT IT INTO THE miniSD CARD HOLDER, THERE'S ENOUGH SPACE BETWEEN CASE AND HOLDER TO ACCIDENTALLY PUT IT THERE INSTEAD FORCING YOU TO OPEN THE CASE TO GET IT BACK

Now you just need to reconnect the Raspberry Pi again. Once it boots up, you can easily verify that it's online by SSH:ing to the device. Once it responds on port 22 (SSH) you should be able to connect to port 80 (http) using your webbrowser. Honestly, the bootup is so fast that you can most likely just try accessing the web server. But it's a good trick to know if you need to see if it's up and running.


