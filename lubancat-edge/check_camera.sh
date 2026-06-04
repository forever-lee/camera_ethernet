#!/usr/bin/env bash
set -euo pipefail

CAMERA_IP="${CAMERA_IP:-192.168.4.88}"
ping -c 4 "$CAMERA_IP"
