#!/bin/sh
set -eu

hermes_home="${HERMES_HOME:-/opt/data}"
install -d -o hermes -g hermes "$hermes_home"
install -o hermes -g hermes -m 0600 /opt/doorbell-hermes/config.yaml "$hermes_home/config.yaml"
