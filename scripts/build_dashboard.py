"""Generates grafana/dashboards/aeronode.json.

The dashboard is kept as code so panels and queries are reviewable in diffs
instead of being hand-edited JSON. Run:  python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "influxdb", "uid": "influxdb-aeronode"}
BUCKET = "telemetry"
DRONE_FILTER = '|> filter(fn: (r) => contains(value: r.drone_id, set: ${drone:json}))'
RANGE = "|> range(start: v.timeRangeStart, stop: v.timeRangeStop)"

_next_id = 0


def pid() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def target(query: str, ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DS, "query": query.strip()}


def series_query(field: str) -> str:
    return f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "telemetry" and r._field == "{field}")
  {DRONE_FILTER}
  |> group(columns: ["drone_id"])
  |> sort(columns: ["_time"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{_time: r._time, _value: r._value, drone_id: r.drone_id}}))
'''


def timeseries(title: str, field: str, unit: str, x: int, y: int, w: int = 12, h: int = 8,
               thresholds: list[tuple[str, float | None]] | None = None, minv=None, maxv=None,
               description: str = "") -> dict:
    steps = [{"color": c, "value": v} for c, v in (thresholds or [("green", None)])]
    return {
        "id": pid(), "type": "timeseries", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(series_query(field))],
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": minv, "max": maxv,
                "displayName": "${__field.labels.drone_id}",
                "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never", "spanNulls": 5000,
                           "thresholdsStyle": {"mode": "line+area" if thresholds else "off"}},
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat(title: str, query: str, unit: str, x: int, y: int, w: int = 6, h: int = 4,
         steps: list[tuple[str, float | None]] | None = None, decimals: int | None = None) -> dict:
    return {
        "id": pid(), "type": "stat", "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(query)],
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in (steps or [("green", None)])]},
            "color": {"mode": "thresholds"},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "value", "justifyMode": "center"},
    }


def engine_field(field: str) -> str:
    return f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "engine" and r._field == "{field}")
  |> keep(columns: ["_time", "_value"])
'''


SEVERITY_MAPPINGS = [{
    "type": "value",
    "options": {
        "critical": {"color": "red", "index": 0, "text": "CRITICAL"},
        "warning": {"color": "orange", "index": 1, "text": "WARNING"},
        "info": {"color": "blue", "index": 2, "text": "info"},
    },
}]

MODE_MAPPINGS = [{
    "type": "value",
    "options": {
        "MISSION": {"color": "green", "index": 0},
        "HOLD": {"color": "yellow", "index": 1},
        "DESCEND": {"color": "yellow", "index": 2},
        "RTL": {"color": "orange", "index": 3},
        "LAND": {"color": "red", "index": 4},
        "LANDED": {"color": "text", "index": 5},
    },
}]


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # ---- Row 1: headline numbers
    panels += [
        stat("Drones online", engine_field("drones_online"), "none", 0, y,
             steps=[("red", None), ("orange", 1), ("green", 3)], decimals=0),
        stat("Telemetry throughput", engine_field("frames_per_s"), "reqps", 6, y, decimals=1),
        stat("Decision latency p95", engine_field("latency_ms_p95"), "ms", 12, y,
             steps=[("green", None), ("orange", 5), ("red", 20)], decimals=2),
        stat("Critical events", f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "events" and r._field == "message" and r.severity == "critical")
  {DRONE_FILTER}
  |> group()
  |> count()
''', "none", 18, y, steps=[("green", None), ("red", 1)], decimals=0),
    ]
    y += 4

    # ---- Row 2: map + fleet status
    panels.append({
        "id": pid(), "type": "geomap", "title": "Live fleet map", "datasource": DS,
        "gridPos": {"x": 0, "y": y, "w": 12, "h": 12},
        "targets": [target(f'''
from(bucket: "{BUCKET}")
  |> range(start: -1m)
  |> filter(fn: (r) => r._measurement == "telemetry" and (r._field == "lat" or r._field == "lon" or r._field == "battery" or r._field == "alt"))
  {DRONE_FILTER}
  |> group(columns: ["drone_id", "_field"])
  |> last()
  |> pivot(rowKey: ["drone_id"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> keep(columns: ["drone_id", "lat", "lon", "battery", "alt"])
''')],
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None}, {"color": "orange", "value": 25}, {"color": "green", "value": 40}]},
        }, "overrides": []},
        "options": {
            "view": {"id": "coords", "lat": 17.4474, "lon": 78.3762, "zoom": 14.5, "allLayers": True},
            "controls": {"showZoom": True, "mouseWheelZoom": True, "showAttribution": True},
            "basemap": {"type": "default", "name": "Basemap"},
            "layers": [{
                "type": "markers", "name": "Drones", "tooltip": True,
                "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
                "config": {
                    "showLegend": False,
                    "style": {
                        "size": {"fixed": 9},
                        "color": {"field": "battery", "fixed": "green"},
                        "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
                        "text": {"mode": "field", "field": "drone_id", "fixed": ""},
                        "textConfig": {"fontSize": 12, "offsetY": -16, "textAlign": "center", "textBaseline": "middle"},
                    },
                },
            }],
            "tooltip": {"mode": "details"},
        },
    })
    panels.append({
        "id": pid(), "type": "table", "title": "Fleet status", "datasource": DS,
        "gridPos": {"x": 12, "y": y, "w": 12, "h": 12},
        "targets": [target(f'''
from(bucket: "{BUCKET}")
  |> range(start: -1m)
  |> filter(fn: (r) => r._measurement == "telemetry" and (r._field == "battery" or r._field == "alt" or r._field == "speed" or r._field == "signal" or r._field == "motor_temp" or r._field == "distance_home"))
  {DRONE_FILTER}
  |> group(columns: ["drone_id", "_field"])
  |> last()
  |> pivot(rowKey: ["drone_id", "mode"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> keep(columns: ["drone_id", "mode", "battery", "alt", "speed", "motor_temp", "signal", "distance_home"])
  |> sort(columns: ["drone_id"])
''')],
        "fieldConfig": {"defaults": {"custom": {"align": "center"}}, "overrides": [
            {"matcher": {"id": "byName", "options": "mode"},
             "properties": [{"id": "mappings", "value": MODE_MAPPINGS},
                            {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
            {"matcher": {"id": "byName", "options": "battery"},
             "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0},
                            {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                            {"id": "min", "value": 0}, {"id": "max", "value": 100},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                {"color": "red", "value": None}, {"color": "orange", "value": 25},
                                {"color": "green", "value": 40}]}}]},
            {"matcher": {"id": "byName", "options": "alt"},
             "properties": [{"id": "unit", "value": "lengthm"}, {"id": "decimals", "value": 0}, {"id": "displayName", "value": "altitude"}]},
            {"matcher": {"id": "byName", "options": "speed"},
             "properties": [{"id": "unit", "value": "velocityms"}, {"id": "decimals", "value": 1}]},
            {"matcher": {"id": "byName", "options": "motor_temp"},
             "properties": [{"id": "unit", "value": "celsius"}, {"id": "decimals", "value": 0}, {"id": "displayName", "value": "motor"}]},
            {"matcher": {"id": "byName", "options": "signal"},
             "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "distance_home"},
             "properties": [{"id": "unit", "value": "lengthm"}, {"id": "decimals", "value": 0}, {"id": "displayName", "value": "from home"}]},
        ]},
        "options": {"showHeader": True, "cellHeight": "md"},
    })
    y += 12

    # ---- Row 3: decisions
    panels.append({
        "id": pid(), "type": "table", "title": "Decision & event log", "datasource": DS,
        "gridPos": {"x": 0, "y": y, "w": 16, "h": 10},
        "targets": [target(f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "events" and r._field == "message" and r.type != "MODE_CHANGED")
  {DRONE_FILTER}
  |> group()
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 200)
  |> keep(columns: ["_time", "drone_id", "severity", "type", "action", "_value"])
  |> rename(columns: {{_value: "message"}})
''')],
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "severity"},
             "properties": [{"id": "mappings", "value": SEVERITY_MAPPINGS},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "custom.width", "value": 100}]},
            {"matcher": {"id": "byName", "options": "_time"},
             "properties": [{"id": "displayName", "value": "time"}, {"id": "custom.width", "value": 170}]},
            {"matcher": {"id": "byName", "options": "drone_id"},
             "properties": [{"id": "displayName", "value": "drone"}, {"id": "custom.width", "value": 80}]},
            {"matcher": {"id": "byName", "options": "type"}, "properties": [{"id": "custom.width", "value": 210}]},
            {"matcher": {"id": "byName", "options": "action"}, "properties": [{"id": "custom.width", "value": 110}]},
        ]},
        "options": {"showHeader": True, "cellHeight": "sm"},
    })
    panels.append({
        "id": pid(), "type": "barchart", "title": "Events by type", "datasource": DS,
        "gridPos": {"x": 16, "y": y, "w": 8, "h": 10},
        "targets": [target(f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "events" and r._field == "message" and r.type != "MODE_CHANGED" and r.type != "COMMAND_SENT")
  {DRONE_FILTER}
  |> group(columns: ["type"])
  |> count()
  |> group()
  |> keep(columns: ["type", "_value"])
  |> rename(columns: {{_value: "events"}})
  |> sort(columns: ["events"], desc: true)
''')],
        "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": "#5794F2"},
                                     "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
        "options": {"orientation": "horizontal", "xField": "type", "showValue": "auto",
                    "legend": {"showLegend": False}, "barWidth": 0.7, "groupWidth": 0.7},
    })
    y += 10

    # ---- Row 4/5: telemetry
    panels.append(timeseries("Battery", "battery", "percent", 0, y, minv=0, maxv=100,
                             thresholds=[("red", None), ("orange", 12), ("transparent", 25)]))
    panels.append(timeseries("Altitude", "alt", "lengthm", 12, y, minv=0,
                             thresholds=[("transparent", None), ("red", 120)],
                             description="Raw sensor readings: glitch spikes show here even though the engine ignores them."))
    y += 8
    panels.append(timeseries("Motor temperature", "motor_temp", "celsius", 0, y,
                             thresholds=[("transparent", None), ("orange", 70), ("red", 85)]))
    panels.append(timeseries("Distance from home", "distance_home", "lengthm", 12, y, minv=0,
                             thresholds=[("transparent", None), ("orange", 850), ("red", 1000)]))
    y += 8

    # ---- Row 6: prediction + engine health
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Predicted battery margin (time-to-reserve minus time-to-home)",
        "description": "Below zero the drone could not get home on its current drain rate; the engine returns it before the safety factor is crossed.",
        "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 12, "h": 8},
        "targets": [target(f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "battery_forecast" and (r._field == "seconds_to_reserve" or r._field == "seconds_to_home"))
  {DRONE_FILTER}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({{r with _value: r.seconds_to_reserve - r.seconds_to_home}}))
  |> group(columns: ["drone_id"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "drone_id"])
''')],
        "fieldConfig": {"defaults": {"unit": "s", "displayName": "${__field.labels.drone_id}",
                                     "custom": {"lineWidth": 2, "fillOpacity": 5, "showPoints": "never",
                                                "thresholdsStyle": {"mode": "line+area"}},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "red", "value": None}, {"color": "transparent", "value": 0}]}},
                        "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    })
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Engine performance", "datasource": DS,
        "gridPos": {"x": 12, "y": y, "w": 12, "h": 8},
        "targets": [target(f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "engine" and (r._field == "latency_ms_p95" or r._field == "latency_ms_p50" or r._field == "frames_per_s"))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''')],
        "fieldConfig": {"defaults": {"custom": {"lineWidth": 2, "fillOpacity": 5, "showPoints": "never"}},
                        "overrides": [
                            {"matcher": {"id": "byRegexp", "options": ".*latency.*"},
                             "properties": [{"id": "unit", "value": "ms"}]},
                            {"matcher": {"id": "byRegexp", "options": ".*frames_per_s.*"},
                             "properties": [{"id": "unit", "value": "reqps"},
                                            {"id": "custom.axisPlacement", "value": "right"}]},
                        ]},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi"}},
    })

    return {
        "uid": "aeronode-fleet",
        "title": "AeroNode - Fleet Operations",
        "tags": ["aeronode", "uav", "telemetry"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "5s",
        "time": {"from": "now-15m", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m"]},
        "schemaVersion": 39,
        "version": 1,
        "templating": {"list": [{
            "name": "drone", "label": "Drone", "type": "query", "datasource": DS,
            "query": f'import "influxdata/influxdb/schema"\nschema.tagValues(bucket: "{BUCKET}", tag: "drone_id")',
            "definition": "drone_id tag values",
            "multi": True, "includeAll": True,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "refresh": 2, "sort": 1,
        }]},
        "annotations": {"list": [{
            "name": "Critical events", "enable": True, "iconColor": "red", "datasource": DS,
            "target": {"refId": "Anno", "query": f'''
from(bucket: "{BUCKET}")
  {RANGE}
  |> filter(fn: (r) => r._measurement == "events" and r._field == "message" and r.severity == "critical")
  |> map(fn: (r) => ({{_time: r._time, _value: r.drone_id + ": " + r._value}}))
'''.strip()},
        }]},
        "panels": panels,
    }


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "aeronode.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
