#!/bin/sh
set -eu
# Standalone local node: no public bootstrap or multicast. Only encrypted evidence is added.
ipfs bootstrap rm --all
ipfs config --json Discovery.MDNS.Enabled false
ipfs config Routing.Type none
ipfs config --json AutoTLS.Enabled false
ipfs config --json Addresses.Swarm '[]'
