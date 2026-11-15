"""Compact holographic status panel for the Raspberry Pi smart-home bridge."""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from gui.style import hud_font
from gui.theme import C


class SmartHomeOverlay(QWidget):
    _snapshot_ready = pyqtSignal(object)
    _operation_done = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background: rgba(0, 7, 18, 0.98); border: 1px solid {C.BORDER_B}; "
            "border-radius: 12px;")
        self._refreshing = False
        self._rows: list[QWidget] = []
        self._last_operation = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("SMART HOME  /  RASPBERRY PI")
        title.setFont(hud_font(11, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        header.addWidget(title)
        header.addStretch()
        self._status = QLabel("Checking connection…")
        self._status.setFont(hud_font(8))
        self._status.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        header.addWidget(self._status)
        root.addLayout(header)

        self._detail = QLabel("Configure the outgoing COM port in Plugin Settings.")
        self._detail.setWordWrap(True)
        self._detail.setFont(hud_font(8))
        self._detail.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        root.addWidget(self._detail)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(5)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        buttons = QHBoxLayout()
        self._refresh_button = QPushButton("REFRESH")
        self._refresh_button.clicked.connect(self.refresh)
        self._all_off_button = QPushButton("ALL LIGHTS OFF")
        self._all_off_button.clicked.connect(self._all_lights_off)
        close_button = QPushButton("CLOSE")
        close_button.clicked.connect(self.hide)
        for button in (self._refresh_button, self._all_off_button, close_button):
            button.setFixedHeight(30)
            button.setFont(hud_font(8, QFont.Weight.Bold))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ color: {C.PRI}; background: #00091a; "
                f"border: 1px solid {C.PRI_DIM}; border-radius: 3px; padding: 3px 8px; }}"
                f"QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}")
            buttons.addWidget(button)
        root.addLayout(buttons)

        self._snapshot_ready.connect(self._render_snapshot)
        self._operation_done.connect(self._show_operation)
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self.refresh()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._status.setText("Checking…")

        def worker():
            try:
                from plugins.smart_home import dashboard_snapshot
                result = dashboard_snapshot()
            except Exception as exc:
                result = {"connected": False, "error": str(exc), "entities": []}
            self._snapshot_ready.emit(result)

        threading.Thread(target=worker, name="smart-home-ui-refresh",
                         daemon=True).start()

    def _render_snapshot(self, snapshot):
        self._refreshing = False
        connected = bool(snapshot.get("connected"))
        entities = snapshot.get("entities") or []
        self._status.setText(
            f"BT / PI / HA: CONNECTED · {len(entities)} DEVICES"
            if connected else "BT / PI / HA: DISCONNECTED")
        self._status.setStyleSheet(
            f"color: {C.PRI if connected else '#ff6b6b'}; background: transparent;")
        detail = snapshot.get("detail") or snapshot.get("error") \
            or "Devices and sensor states are loaded from Home Assistant."
        if self._last_operation:
            detail += "\nLast command: " + self._last_operation
        self._detail.setText(detail)
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()

        for item in entities:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("entity_id") or "")
            domain = eid.split(".", 1)[0]
            state = str(item.get("state", "unknown"))
            attributes = item.get("attributes")
            if isinstance(attributes, dict):
                unit = attributes.get("unit_of_measurement")
                if unit:
                    state += f" {unit}"
                for key, label, suffix in (
                    ("current_temperature", " · ", " °C"),
                    ("humidity", " · ", "%"),
                ):
                    if key in attributes:
                        state += f"{label}{attributes[key]}{suffix}"
            row = QWidget()
            row.setStyleSheet(
                f"background: rgba(0, 22, 38, 0.75); border: 1px solid {C.BORDER}; "
                "border-radius: 4px;")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(8, 4, 8, 4)
            label = QLabel(
                f"{item.get('friendly_name') or eid}  ·  {state}")
            label.setWordWrap(True)
            label.setFont(hud_font(8))
            label.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
            layout.addWidget(label, 1)
            if domain in ("light", "switch"):
                for action, text in (("turn_on", "ON"), ("turn_off", "OFF")):
                    button = QPushButton(text)
                    button.setFixedSize(44, 24)
                    button.setFont(hud_font(7, QFont.Weight.Bold))
                    button.setStyleSheet(
                        f"QPushButton {{ color: {C.PRI}; background: #00091a; "
                        f"border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}"
                        f"QPushButton:hover {{ background: {C.PRI_GHO}; }}")
                    button.clicked.connect(
                        lambda _=False, a=action, entity=eid: self._control(a, entity))
                    layout.addWidget(button)
            self._content_layout.addWidget(row)
            self._rows.append(row)
        if not entities:
            label = QLabel(snapshot.get("error") or "No Home Assistant entities found.")
            label.setFont(hud_font(9))
            label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._content_layout.addWidget(label)
            self._rows.append(label)
        self._content_layout.addStretch(1)

    def _control(self, action: str, entity_id: str):
        self._run_operation({"action": action, "entity_id": entity_id})

    def _all_lights_off(self):
        self._run_operation({"action": "turn_off_all_lights"})

    def _run_operation(self, parameters: dict):
        self._status.setText("Sending…")

        def worker():
            from plugins.smart_home import run
            result = run(parameters)
            self._operation_done.emit(str(result))

        threading.Thread(target=worker, name="smart-home-ui-command",
                         daemon=True).start()

    def _show_operation(self, message: str):
        self._last_operation = message
        self.refresh()
