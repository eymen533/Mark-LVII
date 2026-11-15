# U.Ş.A.K Raspberry Pi / Home Assistant bridge

This module sends a small allow-listed command set from the Windows assistant to
the Pi over the HC-06 Bluetooth Classic serial connection. Home Assistant's
long-lived token stays on the Pi; it is not sent over Bluetooth or stored on the
Windows computer.

## Raspberry Pi 4 setup

1. Wire the HC-06's UART TX/RX to the Pi's 3.3 V UART (cross TX/RX, common
   ground; do not connect 5 V UART signals). Enable the serial hardware in
   Raspberry Pi configuration and disable the serial login console. Confirm
   that `/dev/serial0` exists and that the HC-06 is configured for 9600 baud.
   Pair the HC-06 with the Windows PC; change its default pairing PIN if its
   firmware supports that setting.
2. Copy `raspberry_pi_bridge.py` to `/opt/usak-smart-home/`. Install Python and
   pyserial in a virtual environment:

   ```sh
   sudo apt update
   sudo apt install -y python3-venv
   sudo mkdir -p /opt/usak-smart-home /etc/usak
   sudo python3 -m venv /opt/usak-smart-home/.venv
   sudo /opt/usak-smart-home/.venv/bin/pip install 'pyserial>=3.5,<4'
   ```

3. Create a dedicated service account and grant it access to the serial device:

   ```sh
   sudo useradd --system --user-group --no-create-home --groups dialout usak
   ```

   Copy `home_assistant.example.json` to `/etc/usak/home_assistant.json`, set
   your Home Assistant URL and long-lived access token, then restrict access:

   ```sh
   sudo chown root:usak /etc/usak/home_assistant.json
   sudo chmod 640 /etc/usak/home_assistant.json
   ```

4. Copy `usak-ha-bridge.service` to
   `/etc/systemd/system/usak-ha-bridge.service`, then enable it:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now usak-ha-bridge
   sudo systemctl status usak-ha-bridge
   ```

If a service account already exists, do not create it again; add that account to
the `dialout` group instead. Check `journalctl -u usak-ha-bridge -f` for
connection errors. Logs never include the Home Assistant token.

The HC-06 does not provide a standard baud-rate discovery protocol. Its UART
speed and the Pi service speed must match; select the configured speed in
U.Ş.A.K's plugin settings rather than relying on automatic baud detection.

## Windows assistant setup

Pair the HC-06 in Windows Bluetooth settings and note its **outgoing** COM port.
Open U.Ş.A.K → Setup → Plugin Settings → Raspberry Pi / Bluetooth. Leave the
port on `auto` to probe COM8/COM9 and discovered serial ports, or enter the
outgoing port manually. Use 9600 baud unless the module was configured
differently. The assistant reconnects while running; select **Test Raspberry Pi
Connection** to check both the serial link and Home Assistant.

The assistant tool only changes a discovered `light.*` or `switch.*` entity.
When a room name matches several devices, U.Ş.A.K reports the choices rather
than guessing. Sensor and climate states are read-only.

## Protocol

UTF-8 JSON messages are terminated by `\\n` and include an ID for matching the
response. Supported operations: `status`, `list_entities`, `get_state`,
`turn_on`, `turn_off`, `toggle`, and `turn_off_all_lights`. Arbitrary shell
commands, services, and entity IDs are never forwarded.

Hardware operation cannot be verified without a paired HC-06 and the configured
Home Assistant instance.
