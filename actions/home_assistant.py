
from __future__ import annotations

import json
import urllib.request
import urllib.error
from pathlib import Path

HA_URL = "http://192.168.1.131:8123"
TOKEN_FILE = Path(__file__).with_name("ha_token.txt")

DEFAULT_ENTITY = "light.yeelight_color_0x7d45b82"

COLORS = {
    "kirmizi": [255, 0, 0],
    "kırmızı": [255, 0, 0],
    "yesil": [0, 255, 0],
    "yeşil": [0, 255, 0],
    "mavi": [0, 0, 255],
    "mor": [160, 0, 255],
    "beyaz": [255, 255, 255],
    "sari": [255, 255, 0],
    "sarı": [255, 255, 0],
    "turuncu": [255, 100, 0],
    "pembe": [255, 20, 147],
}


def _get_token() -> str:
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            "Token dosyası bulunamadı: actions/ha_token.txt"
        )

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if not token:
        raise ValueError("Home Assistant token dosyası boş.")

    return token


def ha_request(endpoint, method="GET", data=None):
    """Home Assistant REST API üzerinden JSON isteği gönderir."""
    token = _get_token()

    if not str(endpoint).startswith("/api/"):
        raise ValueError("Geçersiz Home Assistant API endpoint'i.")

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    request = urllib.request.Request(
        HA_URL + endpoint,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else None

    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Home Assistant HTTP {error.code}: {detail}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Home Assistant bağlantı hatası: {error}"
        ) from error


def get_camera_image(entity_id: str) -> bytes:
    """Home Assistant üzerinden kamera JPEG görüntüsünü indirir."""
    entity_id = str(entity_id or "").strip()

    if not entity_id.startswith("camera."):
        raise ValueError("Geçerli bir camera entity_id gerekli.")

    token = _get_token()

    request = urllib.request.Request(
        f"{HA_URL}/api/camera_proxy/{entity_id}",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "image/*",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")

            if not content_type.startswith("image/"):
                raise RuntimeError(
                    f"Kamera görüntüsü yerine {content_type} döndü."
                )

            image_data = response.read()

            if not image_data:
                raise RuntimeError("Kamera boş görüntü döndürdü.")

            return image_data

    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Kamera HTTP {error.code}: {detail}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Kamera bağlantı hatası: {error}"
        ) from error


def get_cameras() -> list[dict]:
    """Home Assistant içindeki bütün kamera varlıklarını bulur."""
    states = ha_request("/api/states")

    if not isinstance(states, list):
        raise RuntimeError("Home Assistant geçersiz cihaz listesi döndürdü.")

    cameras = []

    for item in states:
        if not isinstance(item, dict):
            continue

        entity_id = str(item.get("entity_id") or "")

        if not entity_id.startswith("camera."):
            continue

        attributes = item.get("attributes") or {}

        cameras.append({
            "entity_id": entity_id,
            "friendly_name": attributes.get("friendly_name", entity_id),
            "state": item.get("state", "unknown"),
            "attributes": attributes,
        })

    return cameras


def control_home_assistant(parameters=None, **kwargs):
    parameters = parameters or {}

    action = str(parameters.get("action", "")).lower()
    entity = parameters.get("entity_id") or DEFAULT_ENTITY

    try:
        if action == "list_lights":
            states = ha_request("/api/states")

            lights = [
                item for item in states
                if item.get("entity_id", "").startswith("light.")
            ]

            if not lights:
                return "Home Assistant'ta ışık bulunamadı."

            lines = ["Bulunan ışıklar:"]

            for light in lights:
                name = light.get("attributes", {}).get(
                    "friendly_name", "İsimsiz"
                )
                lines.append(
                    f"{name} | {light['entity_id']} | {light['state']}"
                )

            return "\n".join(lines)

        if action == "list_cameras":
            cameras = get_cameras()

            if not cameras:
                return "Home Assistant'ta kamera bulunamadı."

            return "Bulunan kameralar:\n" + "\n".join(
                f"{camera['friendly_name']} | "
                f"{camera['entity_id']} | {camera['state']}"
                for camera in cameras
            )

        if action == "status":
            state = ha_request("/api/states/" + entity)
            name = state.get("attributes", {}).get(
                "friendly_name", entity
            )
            return f"{name} şu anda: {state.get('state')}"

        if action in ("on", "off", "toggle"):
            service = {
                "on": "turn_on",
                "off": "turn_off",
                "toggle": "toggle",
            }[action]

            data = {"entity_id": entity}

            if action == "on":
                color = parameters.get("color")

                if color:
                    rgb = COLORS.get(str(color).lower())
                    if rgb:
                        data["rgb_color"] = rgb

                brightness = parameters.get("brightness_pct")

                if brightness is not None:
                    data["brightness_pct"] = max(
                        1, min(100, int(brightness))
                    )

            ha_request(
                "/api/services/light/" + service,
                method="POST",
                data=data,
            )

            names = {
                "on": "açıldı",
                "off": "kapatıldı",
                "toggle": "durumu değiştirildi",
            }

            return f"Işık {names[action]}: {entity}"

        return "Bilinmeyen işlem: " + action

    except Exception as error:
        return "Home Assistant hatası: " + str(error)


TOOL = {
    "name": "control_home_assistant",
    "description": (
        "Home Assistant akıllı ev sistemini yerel Wi-Fi üzerinden kontrol "
        "eder. Işıkları listeleyebilir, açabilir, kapatabilir, renk ve "
        "parlaklık ayarlayabilir. Kameraları listeleyebilir."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "list_lights",
                    "list_cameras",
                    "status",
                    "on",
                    "off",
                    "toggle",
                ],
                "description": "Yapılacak işlem",
            },
            "entity_id": {
                "type": "STRING",
                "description": "Kontrol edilecek cihazın entity ID'si",
            },
            "color": {
                "type": "STRING",
                "description": "Işık rengi",
            },
            "brightness_pct": {
                "type": "INTEGER",
                "description": "Parlaklık yüzdesi: 1-100",
            },
        },
        "required": ["action"],
    },
    "handler": control_home_assistant,
}
