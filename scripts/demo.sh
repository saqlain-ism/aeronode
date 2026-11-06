#!/usr/bin/env bash
# Guided fault-injection demo. Breaks things on purpose and shows how AeroNode reacts.
#
#   ./scripts/demo.sh             run every scenario (~8 minutes)
#   ./scripts/demo.sh intercept   run one: spike stuck battery flyaway climb heat intercept link
#
# Keep Grafana open next to the terminal: http://localhost:3000

set -euo pipefail

API="${API:-http://localhost:8000}"
KEY_HEADER=()
[[ -n "${API_KEY:-}" ]] && KEY_HEADER=(-H "X-API-Key: ${API_KEY}")

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# Print a drone id that is flying a mission (highest battery first), excluding any given ids.
pick_flying() {
  curl -fsS "${API}/drones" | python3 -c '
import json, sys
exclude = set(sys.argv[1:])
flying = [d for d in json.load(sys.stdin) if d["mode"] == "MISSION" and d["link_up"] and d["drone_id"] not in exclude]
flying.sort(key=lambda d: -d["telemetry"]["battery"])
print(flying[0]["drone_id"] if flying else "")' "$@"
}

# Print "interceptor target" for the two flying drones closest to each other.
pick_pair() {
  curl -fsS "${API}/drones" | python3 -c '
import itertools, json, math, sys
fly = [d for d in json.load(sys.stdin) if d["mode"] == "MISSION" and d["link_up"]]
def dist(a, b):
    ta, tb = a["telemetry"], b["telemetry"]
    dy = (ta["lat"] - tb["lat"]) * 111_320
    dx = (ta["lon"] - tb["lon"]) * 111_320 * math.cos(math.radians(ta["lat"]))
    return math.hypot(dx, dy)
pairs = sorted(itertools.combinations(fly, 2), key=lambda p: dist(*p))
print(" ".join(d["drone_id"] for d in pairs[0]) if pairs else "")'
}

inject() {  # drone fault [duration] [target]
  local body="{\"type\":\"$2\""
  [[ -n "${3:-}" ]] && body+=",\"duration\":$3"
  [[ -n "${4:-}" ]] && body+=",\"target\":\"$4\""
  body+="}"
  curl -fsS -X POST "${API}/drones/$1/fault" -H 'Content-Type: application/json' "${KEY_HEADER[@]}" -d "$body" >/dev/null
  note "-> injected '$2' on $1${4:+ (target $4)}"
}

# watch <drones,comma,separated> <since> <timeout_s> <event type that ends the scenario>
watch_events() {
  local drones=$1 since=$2 timeout=$3 until=$4 waited=0
  note "watching (up to ${timeout}s, until ${until})..."
  while (( waited < timeout )); do
    if curl -fsS "${API}/events?drone_id=${drones}&since=${since}&limit=200" | grep -q "\"${until}\""; then
      sleep 2; break
    fi
    sleep 2; waited=$((waited + 2))
  done
  curl -fsS "${API}/events?drone_id=${drones}&since=${since}&limit=50" | python3 -c '
import datetime, json, sys
for e in reversed(json.load(sys.stdin)):
    t = datetime.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
    drone, sev, typ, msg = e["drone_id"], e["severity"], e["type"], e["message"]
    print(f"  {t}  {drone}  {sev:<8} {typ:<24} {msg}")'
}

run() {  # title explanation fault until timeout
  local title=$1 fault=$2 until=$3 timeout=$4 drone since
  bold "$title"
  drone=$(pick_flying)
  [[ -z "$drone" ]] && { note "no drone on mission right now - skipping"; return; }
  since=$(date +%s)
  inject "$drone" "$fault"
  watch_events "$drone" "$since" "$timeout" "$until"
}

scenario_spike() {
  run "1. Glitchy altimeter -> bad readings ignored, sensor reset, recovered" \
      sensor_spike RECOVERY_SUCCEEDED 40
}

scenario_stuck() {
  run "2. Stubborn sensor -> recovery ladder: RESET_SENSOR, then REBOOT" \
      sensor_stuck RECOVERY_SUCCEEDED 60
}

scenario_battery() {
  run "3. Battery cell failure -> sudden drop detected, predictive return-to-home" \
      battery_drop COMMAND_SENT 30
}

scenario_flyaway() {
  run "4. Flyaway -> geofence warning, breach, automatic RTL" \
      flyaway GEOFENCE_BREACH 150
}

scenario_climb() {
  run "5. Altitude runaway -> 120 m ceiling breached, DESCEND" \
      altitude_runaway ALTITUDE_LIMIT 90
}

scenario_heat() {
  run "6. Motor overheating -> warning, then emergency LAND" \
      motor_overheat MOTOR_TEMP_CRITICAL 60
}

scenario_intercept() {
  bold "7. Mid-air conflict -> faster-closing drone HOLDs, resumes once clear"
  local pair since
  pair=$(pick_pair)
  [[ -z "$pair" ]] && { note "need two drones on mission - skipping"; return; }
  read -r a b <<<"$pair"
  since=$(date +%s)
  inject "$a" intercept 150 "$b"
  watch_events "$a,$b" "$since" 170 PROXIMITY_CLEARED
}

scenario_link() {
  bold "8. Lost link -> watchdog raises LINK_LOST, measures downtime on return"
  local drone since
  drone=$(pick_flying)
  since=$(date +%s)
  inject "$drone" signal_loss 12
  watch_events "$drone" "$since" 30 LINK_RESTORED
}

ALL=(spike stuck battery flyaway climb heat intercept link)

curl -fsS "${API}/health" >/dev/null || { echo "Engine not reachable at ${API}. Run 'make up' first."; exit 1; }

if [[ $# -gt 0 ]]; then
  "scenario_$1"
else
  bold "Waiting for at least 3 drones on mission..."
  for _ in $(seq 1 60); do
    n=$(curl -fsS "${API}/drones" | python3 -c 'import json,sys; print(sum(d["mode"]=="MISSION" for d in json.load(sys.stdin)))')
    (( n >= 3 )) && break
    sleep 3
  done
  for s in "${ALL[@]}"; do "scenario_$s"; done
fi

bold "Engine metrics"
curl -fsS "${API}/metrics" | python3 -m json.tool
