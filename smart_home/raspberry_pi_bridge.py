#!/usr/bin/env python3
"""HC-06 newline-JSON service that exposes a small allow-list of HA operations."""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("usak-ha-bridge")
MAX_LINE = 16384
MAX_ENTITIES = 250
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_ALLOWED_ACTIONS = {
    "status", "list_entities", "get_state",
    "turn_on", "turn_off", "toggle", "turn_off_all_lights",
}


class HomeAssistant:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("Home Assistant URL must use http:// or https://.")
        if not token:
            raise ValueError("Home Assistant long-lived access token is required.")

    def request(self, path: str, method: str = "GET", body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                raw = response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            # Do not log request headers or the long-lived token.
            raise RuntimeError(f"Home Assistant returned HTTP {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Home Assistant is unreachable: {exc.reason if hasattr(exc, 'reason') else exc}") from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Home Assistant returned invalid JSON.") from exc

    def entities(self) -> list[dict]:
        states = self.request("/api/states")
        if not isinstance(states, list):
            raise RuntimeError("Home Assistant returned an invalid states list.")
        result = []
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            if not isinstance(entity_id, str) or not _ENTITY_RE.fullmatch(entity_id):
                continue
            if entity_id.split(".", 1)[0] not in (
                    "light", "switch", "sensor", "binary_sensor", "climate"):
                continue
            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            clean_attrs = {
                key: attrs[key] for key in
                ("unit_of_measurement", "device_class", "temperature",
                 "current_temperature", "target_temperature", "humidity")
                if key in attrs and isinstance(attrs[key], (str, int, float, bool))
            }
            result.append({
                "entity_id": entity_id,
                "state": str(item.get("state") or "unknown")[:120],
                "friendly_name": str(attrs.get("friendly_name") or entity_id)[:120],
                "attributes": clean_attrs,
            })
            if len(result) >= MAX_ENTITIES:
                break
        return result

    def command(self, action: str, entity_id: str = "") -> dict:
        if action not in _ALLOWED_ACTIONS:
            raise ValueError("Unsupported action.")
        if action == "status":
            self.request("/api/")
            return {"message": "Raspberry Pi and Home Assistant are connected."}
        if action == "list_entities":
            return {"data": self.entities(), "message": "Home Assistant devices loaded."}
        if action == "turn_off_all_lights":
            self.request("/api/services/light/turn_off", "POST", {})
            return {"message": "All Home Assistant lights were turned off."}
        if not _ENTITY_RE.fullmatch(entity_id):
            raise ValueError("A valid discovered entity_id is required.")
        domain, _ = entity_id.split(".", 1)
        states = self.entities()
        state = next((item for item in states if item["entity_id"] == entity_id), None)
        if state is None:
            raise ValueError("The entity is not present in the current Home Assistant state list.")
        if action == "get_state":
            attributes = state.get("attributes") or {}
            unit = attributes.get("unit_of_measurement")
            value = f"{state['state']} {unit}" if unit else state["state"]
            extra = []
            for key, label in (("current_temperature", "temperature"),
                               ("temperature", "target temperature"),
                               ("humidity", "humidity")):
                if key in attributes:
                    suffix = " °C" if "temperature" in key else "%"
                    extra.append(f"{label} {attributes[key]}{suffix}")
            if extra:
                value += " (" + ", ".join(extra) + ")"
            return {"data": state, "message": f"{state['friendly_name']}: {value}."}
        if domain not in ("light", "switch"):
            raise ValueError("Only light and switch entities can be changed.")
        if action not in ("turn_on", "turn_off", "toggle"):
            raise ValueError("Unsupported device operation.")
        self.request(f"/api/services/{domain}/{action}", "POST",
                     {"entity_id": entity_id})
        return {"message": f"{state['friendly_name']}: {action} requested."}


def _load_config(path: Path) -> tuple[str, str]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Configuration file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read configuration file: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("Configuration must be a JSON object.")
    url = str(config.get("home_assistant_url") or "").strip()
    token = str(config.get("home_assistant_token") or "").strip()
    return url, token


def serve(port: str, baudrate: int, config_path: Path) -> None:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("Install pyserial with: python3 -m pip install pyserial") from exc
    url, token = _load_config(config_path)
    ha = HomeAssistant(url, token)
    with serial.Serial(port=port, baudrate=baudrate, timeout=0.5,
                       write_timeout=2.0) as connection:
        LOG.info("Listening on %s at %s baud.", port, baudrate)
        while True:
            line = connection.readline(MAX_LINE + 1)
            if not line:
                continue
            request_id = ""
            try:
                if len(line) > MAX_LINE:
                    raise ValueError("Message exceeds the maximum size.")
                payload = json.loads(line.decode("utf-8").strip())
                if not isinstance(payload, dict) or payload.get("type") != "command":
                    raise ValueError("Invalid command envelope.")
                request_id = str(payload.get("id") or "")[:80]
                action = str(payload.get("action") or "")
                result = ha.command(action, str(payload.get("entity_id") or ""))
                response = {
                    "type": "response", "id": request_id,
                    "success": True, **result,
                }
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
                    RuntimeError, OSError) as exc:
                response = {
                    "type": "response", "id": request_id,
                    "success": False, "message": str(exc)[:300],
                }
            try:
                encoded = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                entities = response.get("data")
                while len(encoded) > 60000 and isinstance(entities, list) and entities:
                    entities.pop()
                    response["truncated"] = True
                    response["message"] = (
                        "Device list shortened to fit the Bluetooth serial response.")
                    encoded = (
                        json.dumps(response, ensure_ascii=False) + "\n"
                    ).encode("utf-8")
                connection.write(encoded)
                connection.flush()
            except (OSError, serial.SerialException) as exc:
                LOG.warning("Serial write failed: %s", exc)
                time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--config", type=Path,
                        default=Path("/etc/usak/home_assistant.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        serve(args.port, args.baudrate, args.config)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
