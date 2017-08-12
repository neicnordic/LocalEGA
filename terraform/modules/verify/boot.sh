#!/bin/bash

set -e

git clone https://github.com/NBISweden/LocalEGA.git ~/ega
pip3.6 install -e ~/ega/src

echo "Waiting for Message Broker"
until nc -4 --send-only ega-mq 5672 </dev/null &>/dev/null; do sleep 1; done

echo "Starting the verifier"
ega-verify &
