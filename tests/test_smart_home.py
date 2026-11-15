import json
import sys
import threading
import types
import unittest
from unittest.mock import patch

from plugins.smart_home import _select_entity
from plugins._whatsapp_core import WindowsDesktopTransport
from smart_home.bridge import SerialBridgeClient
from smart_home.raspberry_pi_bridge import HomeAssistant


class HomeAssistantBridgeTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistant("http://192.168.1.131:8123", "test-token")

    def test_only_supported_entity_domains_are_listed(self):
        with patch.object(self.ha, "request", return_value=[
            {"entity_id": "light.salon", "state": "on",
             "attributes": {"friendly_name": "Salon"}},
            {"entity_id": "sensor.room_temp", "state": "20",
             "attributes": {"unit_of_measurement": "°C"}},
            {"entity_id": "lock.front_door", "state": "locked",
             "attributes": {}},
        ]):
            entities = self.ha.entities()

        self.assertEqual(
            [item["entity_id"] for item in entities],
            ["light.salon", "sensor.room_temp"])

    def test_control_requires_a_discovered_light_or_switch(self):
        with patch.object(self.ha, "entities", return_value=[
                {"entity_id": "sensor.room_temp", "friendly_name": "Room temperature",
                 "state": "20"}]):
            with self.assertRaisesRegex(ValueError, "not present"):
                self.ha.command("turn_on", "light.not_discovered")

            with self.assertRaisesRegex(ValueError, "Only light and switch"):
                self.ha.command("turn_on", "sensor.room_temp")

    def test_light_control_calls_only_the_allow_list_service(self):
        with patch.object(self.ha, "entities", return_value=[
                {"entity_id": "light.salon", "friendly_name": "Salon", "state": "off"}]), \
             patch.object(self.ha, "request") as request:
            result = self.ha.command("turn_on", "light.salon")

        request.assert_called_once_with(
            "/api/services/light/turn_on", "POST", {"entity_id": "light.salon"})
        self.assertIn("Salon", result["message"])

    def test_status_uses_home_assistant_api_endpoint(self):
        with patch.object(self.ha, "request") as request:
            self.ha.command("status")
        request.assert_called_once_with("/api/")


class SmartHomeResolutionTests(unittest.TestCase):
    def test_ambiguous_room_requires_a_choice(self):
        entities = [
            {"entity_id": "light.salon_ceiling", "friendly_name": "Salon ceiling"},
            {"entity_id": "light.salon_lamp", "friendly_name": "Salon lamp"},
        ]
        with self.assertRaisesRegex(ValueError, "More than one device"):
            _select_entity(entities, "salon", "light", "", "turn_on")

    def test_room_and_device_resolve_to_one_discovered_entity(self):
        selected = _select_entity(
            [{"entity_id": "light.salon", "friendly_name": "Salon"}],
            "salon", "ışık", "", "turn_on")
        self.assertEqual(selected["entity_id"], "light.salon")


class SerialProtocolTests(unittest.TestCase):
    def test_response_is_matched_by_request_id(self):
        client = SerialBridgeClient()
        pending = {"event": threading.Event(), "response": None}
        client._pending["request-1"] = pending
        client._handle_line(
            json.dumps({
                "type": "response", "id": "request-1", "success": True,
                "message": "Connected",
            }).encode("utf-8") + b"\n")

        self.assertTrue(pending["event"].is_set())
        self.assertEqual(pending["response"]["message"], "Connected")
        self.assertNotIn("request-1", client._pending)

    def test_malformed_json_does_not_raise(self):
        client = SerialBridgeClient()
        client._handle_line(b"{not-json}\n")
        self.assertIn("malformed JSON", client.status()["last_error"])


class WhatsAppSendVerificationTests(unittest.TestCase):
    class Window:
        def __init__(self):
            self.messages = []

        def elements(self, control_type, fresh=False):
            if control_type != "Text":
                return []
            return [types.SimpleNamespace(name=text) for text in self.messages]

    def _transport(self, window):
        transport = object.__new__(WindowsDesktopTransport)
        patches = [
            patch.object(transport, "_main_window", return_value=window),
            patch.object(transport, "_header_title", return_value="Alice"),
            patch.object(transport, "_focus", return_value=True),
            patch.object(transport, "_message_box", return_value=object()),
            patch.object(transport, "_press", return_value=True),
            patch.object(transport, "_paste_text", return_value=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return transport

    def test_sent_reply_requires_visible_message_not_empty_edit_name(self):
        window = self.Window()
        keyboard = types.ModuleType("pywinauto.keyboard")
        keyboard.send_keys = lambda key: window.messages.append("Hello there") \
            if key == "{ENTER}" else None
        pywinauto = types.ModuleType("pywinauto")
        pywinauto.__path__ = []
        transport = self._transport(window)
        with patch.dict(sys.modules, {
                "pywinauto": pywinauto,
                "pywinauto.keyboard": keyboard,
        }), patch("plugins._whatsapp_core.time.sleep", return_value=None):
            sent, failure = transport.send_message_in_open_chat("Alice", "Hello there")

        self.assertTrue(sent, failure)

    def test_no_visible_outgoing_message_is_not_reported_as_sent(self):
        window = self.Window()
        keyboard = types.ModuleType("pywinauto.keyboard")
        keyboard.send_keys = lambda *_: None
        pywinauto = types.ModuleType("pywinauto")
        pywinauto.__path__ = []
        transport = self._transport(window)
        with patch.dict(sys.modules, {
                "pywinauto": pywinauto,
                "pywinauto.keyboard": keyboard,
        }), patch("plugins._whatsapp_core.time.sleep", return_value=None), \
             patch("plugins._whatsapp_core.time.monotonic", side_effect=[0, 0, 6]):
            sent, failure = transport.send_message_in_open_chat("Alice", "Hello there")

        self.assertFalse(sent)
        self.assertIn("did not confirm", failure)


if __name__ == "__main__":
    unittest.main()
