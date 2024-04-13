#!/bin/bash

# Define some defaults
KEY=id_rsa
HOST=localhost

runcmd() {
	ssh -i $KEY root@$HOST $@
	local RET=$?
	if [ $RET -ne 0 ]; then
		echo "Command failed with $RET"
	fi
	return $RET
}

TEST=true
EXITVALUE=0

HOST=$1
KEY=$2

PROXYSCRIPT="$(dirname "$0")/pika_proxy.py"

if [ ! -f "$PROXYSCRIPT" ]; then
	echo "Proxy script not found, must be in the same directory as this script"
	exit 1
fi

if [ -z "$HOST" -o -z "$KEY" ]; then
	echo "Usage: $0 <host> <key> [test]"
	exit 1
fi

if [ "$3" != "test" ]; then
	TEST=false
fi

# Let's find out if the service is running
if ! curl --silent --connect-timeout 0.5 http://$HOST:8000 >/dev/null; then
	echo "Service does not respond to REST calls"
	EXITVALUE=1
else
	echo "Service is responding"
fi
if $TEST; then
	echo "Test mode, exiting"
	exit $EXITVALUE
fi

# Ensure key is protected or SSH will fail
chmod 600 "$KEY"

if ! runcmd iptables -L INPUT | grep -q 8000; then
	echo "Firewall rule is missing, adding it..."
	runcmd iptables -I INPUT 2 -i eth0 -p tcp --dport 8000 -j ACCEPT
else
	echo "Port 8000 is open"
fi

UPLOAD=false
RESTART=false

if ! runcmd ls | grep -q proxy; then
	echo "Proxy script is missing, uploading..."
	UPLOAD=true
else
	REMOTE_HASH=$(runcmd sha256sum /root/pika_proxy.py | awk '{print $1}')
	LOCAL_HASH=$(sha256sum "$PROXYSCRIPT" | awk '{print $1}')
	echo "Script present"
	if [ "$REMOTE_HASH" != "$LOCAL_HASH" ]; then
		echo "Script is outdated, will upload new"
		echo "Remote: '$REMOTE_HASH'"
		echo "Local : '$LOCAL_HASH'"
		UPLOAD=true
		RESTART=true
	fi
fi

if ! runcmd ps | grep -q proxy; then
	echo "Proxy isn't running, let's see if it's already uploaded"
	RESTART=true
else
	echo "Proxy running"
fi

if $UPLOAD; then
	echo "Uploading script"
	scp -i $KEY "$PROXYSCRIPT" root@$HOST:/root/pika_proxy.py
fi

if $RESTART; then
	# Kill any running instance
	INSTANCE=$(runcmd ps | grep proxy | awk '{print $1}')
	if [ ! -z "$INSTANCE" ]; then
		echo "Killing existing instance of proxy ($INSTANCE)"
		runcmd kill $INSTANCE
	fi
	echo "Starting new instance"
	runcmd './pika_proxy.py >/dev/null 2>/dev/null &'
	echo "Check if it started"
	sleep 1s
	INSTANCE=$(runcmd ps | grep proxy | awk '{print $1}')
	if [ -z $INSTANCE ]; then
		echo "ERROR: Proxy didn't start"
	else
		echo "Proxy running as PID $INSTANCE"
	fi
fi



