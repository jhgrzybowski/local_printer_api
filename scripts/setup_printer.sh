#!/usr/bin/env bash
set -euo pipefail

QUEUE_NAME="${QUEUE_NAME:-Canon_MG5350}"
PRINTER_IP="${PRINTER_IP:-192.168.100.100}"
DEVICE_URI="${DEVICE_URI:-lpd://${PRINTER_IP}/PASSTHRU}"
PPD_DIR="${PPD_DIR:-/etc/cups/ppd}"
FORCE="${FORCE:-0}"
RUN_TEST=0

PREFERRED_MODEL="gutenprint.5.3://bjc-PIXMA-MG5350/expert"
FALLBACK_MODEL_1="gutenprint.5.3://bjc-PIXMA-MG5300/expert"
FALLBACK_MODEL_2="gutenprint.5.3://bjc-MG5300-series/expert"

# Verified fallback transports if PASSTHRU ever stops working:
#   lpd://192.168.100.100/lp
#   lpd://192.168.100.100/print
# Do not default to IPP/everywhere for this printer. Port 631 answers, but this
# MG5350 does not provide the IPP Everywhere attributes CUPS needs.
# Do not use socket://192.168.100.100:9100 unless a probe confirms port 9100 is open.

usage() {
  cat <<EOF
Usage: $0 [--force] [--test]

Environment overrides:
  QUEUE_NAME   default: Canon_MG5350
  PRINTER_IP   default: 192.168.100.100
  DEVICE_URI   default: lpd://192.168.100.100/PASSTHRU
  MODEL        default: auto-detected Gutenprint MG5350/MG5300 model
  PPD_DIR      default: /etc/cups/ppd
  FORCE=1      reconfigure an existing mismatched queue

Options:
  --force      reconfigure an existing mismatched queue
  --test       submit a tiny text test job after setup
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=1
      ;;
    --test)
      RUN_TEST=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Required command not found: $name" >&2
    exit 1
  fi
}

queue_exists() {
  lpstat -p "$QUEUE_NAME" >/dev/null 2>&1
}

current_device_uri() {
  lpstat -v "$QUEUE_NAME" 2>/dev/null | sed -n "s/^device for ${QUEUE_NAME}: //p"
}

detect_model() {
  if [[ -n "${MODEL:-}" ]]; then
    echo "$MODEL"
    return 0
  fi

  local models
  models="$(lpinfo -m)"
  for candidate in "$PREFERRED_MODEL" "$FALLBACK_MODEL_1" "$FALLBACK_MODEL_2"; do
    if grep -Fq "$candidate" <<<"$models"; then
      echo "$candidate"
      return 0
    fi
  done

  cat >&2 <<EOF
No supported Gutenprint MG5350/MG5300 model was found.

Install Gutenprint, then rerun this script:
  sudo apt install -y printer-driver-gutenprint

Expected one of:
  $PREFERRED_MODEL
  $FALLBACK_MODEL_1
  $FALLBACK_MODEL_2
EOF
  exit 1
}

current_model_matches() {
  [[ -n "$expected_nickname" ]] &&
    [[ "$(current_ppd_nickname)" == "$expected_nickname" ]]
}

model_nickname() {
  # lpinfo's description is the NickName embedded in the generated CUPS PPD.
  lpinfo -m | awk -v model="$MODEL" '$1 == model { print substr($0, length($1) + 2); exit }'
}

current_ppd_nickname() {
  local ppd_path="$PPD_DIR/$QUEUE_NAME.ppd"
  [[ -r "$ppd_path" ]] || return 1
  sed -n 's/^\*NickName:[[:space:]]*"\(.*\)"$/\1/p' "$ppd_path"
}

print_status() {
  echo
  echo "== CUPS queue status =="
  lpstat -p "$QUEUE_NAME" -l || true
  echo
  echo "== Device URI =="
  lpstat -v "$QUEUE_NAME" || true
  echo
  echo "== First 80 queue options =="
  lpoptions -p "$QUEUE_NAME" -l 2>&1 | head -80 || true
}

submit_test_print() {
  local test_file="/tmp/canon-test.txt"
  cat > "$test_file" <<EOF
Canon MG5350 CUPS/Gutenprint test
Queue: $QUEUE_NAME
Transport: LPD PASSTHRU
EOF
  lp -d "$QUEUE_NAME" "$test_file"
}

require_command lpadmin
require_command lpstat
require_command lpoptions
require_command lpinfo
require_command cupsaccept
require_command cupsenable
require_command lp

MODEL="$(detect_model)"
expected_nickname="$(model_nickname)"

echo "Queue name: $QUEUE_NAME"
echo "Printer IP: $PRINTER_IP"
echo "Device URI: $DEVICE_URI"
echo "Model: $MODEL"
echo "Force: $FORCE"
echo "Test print: $RUN_TEST"

if queue_exists; then
  existing_uri="$(current_device_uri)"
  if [[ "$existing_uri" == "$DEVICE_URI" ]] && current_model_matches; then
    echo "Queue already exists with expected device URI and model. No changes needed."
    test_submitted="no"
    if [[ "$RUN_TEST" == "1" ]]; then
      submit_test_print
      test_submitted="yes"
    fi
    print_status
    echo
    echo "Summary:"
    echo "  Queue: $QUEUE_NAME"
    echo "  Device URI: $DEVICE_URI"
    echo "  Model: $MODEL"
    echo "  Test job submitted: $test_submitted"
    exit 0
  fi

  echo "Queue already exists but does not match the verified setup."
  echo "Existing URI: ${existing_uri:-unknown}"
  echo "Expected URI: $DEVICE_URI"
  echo "Existing PPD NickName: $(current_ppd_nickname || echo 'unavailable (check PPD permissions)')"
  echo "Expected PPD NickName: ${expected_nickname:-unavailable (model not listed by lpinfo -m)}"

  if [[ "$FORCE" != "1" ]]; then
    cat >&2 <<EOF
No changes made.

Rerun with --force or FORCE=1 to reconfigure the queue:
  sudo $0 --force
EOF
    print_status
    exit 2
  fi

  echo "Force enabled. Recreating queue $QUEUE_NAME."
  lpadmin -x "$QUEUE_NAME"
fi

lpadmin -p "$QUEUE_NAME" -E -v "$DEVICE_URI" -m "$MODEL"
cupsenable "$QUEUE_NAME"
cupsaccept "$QUEUE_NAME"

test_submitted="no"
if [[ "$RUN_TEST" == "1" ]]; then
  submit_test_print
  test_submitted="yes"
fi

echo "Queue configured."
print_status
echo
echo "Summary:"
echo "  Queue: $QUEUE_NAME"
echo "  Device URI: $DEVICE_URI"
echo "  Model: $MODEL"
echo "  Test job submitted: $test_submitted"
