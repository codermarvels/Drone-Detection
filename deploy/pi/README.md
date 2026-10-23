# Raspberry Pi setup

Tested layout: Raspberry Pi 5 (8 GB), Raspberry Pi OS 64-bit, active cooler, HackRF One on a powered USB hub.

```bash
sudo apt install -y hackrf python3-venv git
git clone https://github.com/codermarvels/drone-detection.git ~/drone-detection
cd ~/drone-detection
python3 -m venv .venv && .venv/bin/pip install -e .

# Let the pi user open the HackRF without sudo
sudo cp deploy/pi/53-hackrf.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Train the model on a laptop, then copy models/cnn.onnx and models/cnn.json here.

# Start on boot
sudo cp deploy/pi/dronedet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dronedet
journalctl -u dronedet -f
```

To open the dashboard from another machine, set `web.host: 0.0.0.0` in `config/app.yaml` and browse to `http://<pi-address>:8080`.
