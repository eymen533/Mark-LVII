
"""Home Assistant controls over local Wi-Fi."""
from __future__ import annotations

import re

from actions.home_assistant import ha_request


PLUGIN = {
    "name": "smart_home",
    "description": (
        "Control Home Assistant devices directly over local Wi-Fi. "
        "Supported actions: status, list_entities, get_state, turn_on, "
        "turn_off, toggle, turn_off_all_lights. Only discovered light and "
        "switch entities can be changed. Never invent entity IDs."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "status", "list_entities", "get_state",
                    "turn_on", "turn_off", "toggle",
                    "turn_off_all_lights"
                ],
            },
            "room": {"type": "STRING"},
            "device": {"type": "STRING"},
            "entity_id": {"type": "STRING"},
        },
        "required": ["action"],
    },
    "tier": "core",
    "brief": "Control Home Assistant devices over local Wi-Fi.",
}

PLUGIN_SETTINGS = {
    "namespace": "smart_home",
    "title": "HOME ASSISTANT / WI-FI",
    "fields": [],
    "action": {
        "label": "TEST HOME ASSISTANT CONNECTION",
        "run": lambda values: _test_connection(values),
    },
}

_ACTION_SET = {
    "status", "list_entities", "get_state",
    "turn_on", "turn_off", "toggle",
    "turn_off_all_lights"
}


def _get_entities() -> list[dict]:
    entities = ha_request("/api/states")

    if not isinstance(entities, list):
        raise RuntimeError("Home Assistant returned an invalid entity list.")

    return [
        {
            "entity_id": item["entity_id"],
            "friendly_name": item.get("attributes", {}).get(
                "friendly_name", item["entity_id"]
            ),
            "state": item.get("state", "unknown"),
            "attributes": item.get("attributes", {}),
        }
        for item in entities
        if isinstance(item, dict)
        and isinstance(item.get("entity_id"), str)
    ]


def _fold(value: str) -> str:
    text = str(value or "").lower().translate(str.maketrans({
        "ı": "i", "ş": "s", "ğ": "g",
        "ü": "u", "ö": "o", "ç": "c"
    }))
    return re.sub(r"[^a-z0-9.]+", " ", text).strip()


def _domain(device: str) -> str | None:
    value = _fold(device)

    if "light" in value or "isik" in value or "lamba" in value:
        return "light"
    if "switch" in value or "priz" in value or "plug" in value:
        return "switch"
    if "climate" in value or "thermostat" in value or "termostat" in value:
        return "climate"
    if "sensor" in value or "nem" in value or "temperature" in value:
        return "sensor"
    if "door" in value or "window" in value or "kapi" in value or "pencere" in value:
        return "binary_sensor"

    return None


def _select_entity(
    entities: list[dict],
    room: str,
    device: str,
    entity_id: str,
    action: str
) -> dict:

    if entity_id:
        selected = next(
            (item for item in entities if item["entity_id"] == entity_id),
            None
        )

        if selected is None:
            raise ValueError(
                f"{entity_id} is not a discovered Home Assistant entity."
            )

        return selected

    room_key = _fold(room)

    if not room_key:
        raise ValueError(
            "Provide a discovered entity_id or an unambiguous room."
        )

    domain = _domain(device)

    if action in ("turn_on", "turn_off", "toggle") and domain is None:
        raise ValueError("Specify whether the device is a light or switch.")

    matches = []

    for item in entities:
        eid = item["entity_id"]

        if domain and eid.split(".", 1)[0] != domain:
            continue

        label = _fold(
            str(item.get("friendly_name") or "") + " " + eid
        )

        if room_key in label:
            matches.append(item)

    if not matches:
        raise ValueError(
            f"No matching {device or 'device'} was found for {room}."
        )

    if len(matches) > 1:
        names = [
            str(item.get("friendly_name") or item["entity_id"])
            for item in matches[:8]
        ]
        raise ValueError(
            "More than one device matches; choose one: " + ", ".join(names)
        )

    return matches[0]


def connection_snapshot() -> dict:
    try:
        ha_request("/api/")
        return {
            "connected": True,
            "running": True,
            "detail": "Home Assistant connected over Wi-Fi."
        }
    except Exception as exc:
        return {
            "connected": False,
            "running": False,
            "last_error": str(exc)
        }


def dashboard_snapshot() -> dict:
    try:
        ha_request("/api/")
        entities = _get_entities()

        return {
            "connected": True,
            "detail": "Home Assistant connected over local Wi-Fi.",
            "entities": entities,
        }

    except Exception as exc:
        return {
            "connected": False,
            "error": str(exc),
            "entities": [],
        }


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters if isinstance(parameters, dict) else {}
    action = str(args.get("action") or "").strip()

    if action == "initialize":
        return "Home Assistant Wi-Fi connection initialized."

    if action not in _ACTION_SET:
        return "Unsupported smart-home action."

    try:
        if action == "status":
            ha_request("/api/")
            return "Home Assistant is connected over Wi-Fi."

        if action == "turn_off_all_lights":
            ha_request(
                "/api/services/light/turn_off",
                method="POST",
                data={}
            )
            return "All lights were turned off."

        entities = _get_entities()

        if action == "list_entities":
            if not entities:
                return "Home Assistant returned no entities."

            return "Home Assistant devices: " + "; ".join(
                f"{item['friendly_name']} "
                f"({item['entity_id']}, {item['state']})"
                for item in entities[:40]
            )

        selected = _select_entity(
            entities,
            str(args.get("room") or ""),
            str(args.get("device") or ""),
            str(args.get("entity_id") or ""),
            action
        )

        eid = selected["entity_id"]
        domain = eid.split(".", 1)[0]

        if action == "get_state":
            return (
                f"{selected['friendly_name']} state: "
                f"{selected['state']}"
            )

        if domain not in ("light", "switch"):
            return "Only discovered lights and switches can be changed."

        service = {
            "turn_on": "turn_on",
            "turn_off": "turn_off",
            "toggle": "toggle"
        }[action]

        ha_request(
            f"/api/services/{domain}/{service}",
            method="POST",
            data={"entity_id": eid}
        )

        messages = {
            "turn_on": "turned on",
            "turn_off": "turned off",
            "toggle": "toggled"
        }

        return f"{selected['friendly_name']} {messages[action]}."

    except Exception as exc:
        message = f"Smart-home request failed: {exc}"

        if player is not None and hasattr(player, "write_log"):
            try:
                player.write_log(f"U.Ş.A.K: {message}")
            except Exception:
                pass

        return message


def _test_connection(values: dict) -> tuple[bool, str]:
    try:
        ha_request("/api/")
        return True, "Home Assistant Wi-Fi connection successful."
    except Exception as exc:
        return False, str(exc)


def initialize() -> None:
    print("[smart_home] Home Assistant Wi-Fi integration initialized.")


def stop() -> None:
    pass
