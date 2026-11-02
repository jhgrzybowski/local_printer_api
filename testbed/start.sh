#!/usr/bin/env bash
# Start CUPS and a fake printer, then add two MG5350 queues:
#   Canon_MG5350   prints to the fake LPD printer on 127.0.0.1
#   Canon_Offline  points at an unroutable address, like a printer that is off
set -euo pipefail

mkdir -p /run/cups /var/spool/fake-lpd
cupsd
python3 /testbed/fake_lpd.py 515 >/var/log/fake-lpd.log 2>&1 &
for _ in $(seq 1 50); do lpstat -r >/dev/null 2>&1 && break; sleep 0.1; done

model="$(lpinfo -m | awk '$1 ~ /bjc-PIXMA-MG5350\/expert$/ { print $1; exit }')"
if [[ -z "$model" ]]; then
  echo "Gutenprint MG5350 model not found" >&2
  exit 1
fi
lpadmin -p Canon_MG5350 -E -v lpd://127.0.0.1/PASSTHRU -m "$model" 2>/dev/null
lpadmin -p Canon_Offline -E -v lpd://10.255.255.1/PASSTHRU -m "$model" 2>/dev/null
lpadmin -d Canon_MG5350
echo "testbed ready: $model"

if [[ "${1:-}" == "--wait" ]]; then
  exec sleep infinity
fi
