# Sensor node firmware (ESP32)

One Arduino sketch for both the Wokwi simulation and the real node. It measures temperature and humidity, keeps every reading in a buffer saved to flash, and uploads batches to the backend's `POST /readings` (see [docs/firmware_contract.md](../../docs/firmware_contract.md) §4a). The OLED shows the reading, the buffer size, the link status and the risk level the server answers with.

| File | What it is |
|---|---|
| `blight_node.ino` | The sketch. The sensor sits behind `readClimate()`; `SENSOR` picks DHT22 (Wokwi) or SHT31 (real node). `DEMO_MODE` picks the intervals. |
| `diagram.json` | The Wokwi circuit: ESP32 DevKit, DHT22 on GPIO 15, SSD1306 OLED on I²C (SDA 21, SCL 22), Offline pushbutton on GPIO 4. |
| `libraries.txt` | Libraries for Wokwi (the same names for arduino-cli / Arduino IDE). |
| `secrets.example.h` | The format of `secrets.h`: Wi-Fi, device ID, device key, API URL. |
| `secrets.h` | **Gitignored.** The real values. |

## Behaviour

| | Demo mode | Normal mode (firmware contract) |
|---|---|---|
| Measure | every 15 s | every 10 min |
| Upload | every 30 s | hourly |

- **No reading without a real clock:** nothing is kept until NTP has set the time (contract §7).
- **Buffer first, send second:** each reading goes into a 7-day ring buffer (1,008 readings), saved to LittleFS after every change and restored at boot. When full, the oldest reading is dropped.
- **Uploads:** batches of up to 100, oldest first, with a 90 s timeout (the free Render server takes about a minute to wake). A `200` removes the whole batch from the buffer. `401`, `413` or `422` stops uploads and shows the code on the OLED. Network errors, timeouts and `5xx` keep the data and retry after 1, 2, 4 … 30 min. Reconnecting flushes the buffer at once.
- **Offline button:** press to cut Wi-Fi in code, press again to reconnect. For the demo and the buffer test.
- Never leave demo mode running for days: it would use up the free hosting allowance.

## Run it in Wokwi

1. On [wokwi.com](https://wokwi.com), create a new **ESP32** project (the Arduino template).
2. Paste `blight_node.ino` into `sketch.ino`, and `diagram.json` into `diagram.json`.
3. Add the files `libraries.txt` and `secrets.h` (copy `secrets.example.h`, then put in `wokwi-01`'s key from `device_keys.local.env`). Wokwi projects on the free plan are public, so only `wokwi-01`'s key may go there: it is treated as public and can only add readings to `wokwi-01`.
4. Press ▶. Click the DHT22 to move its sliders.

Wokwi wipes its simulated flash on every restart, so surviving a power cut can only be shown on the real board.

## Compile and flash with arduino-cli

```powershell
arduino-cli core install esp32:esp32
arduino-cli lib install "DHT sensor library for ESPx" "Adafruit SSD1306" "Adafruit GFX Library" "ArduinoJson"
arduino-cli compile --fqbn esp32:esp32:esp32 firmware\blight_node
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 firmware\blight_node    # the board's COM port
```

Currently 85 % of flash and 20 % of RAM.

## Tested (Wokwi, 26 Oct 2026)

| Test | Result |
|---|---|
| Sensor → OLED and serial | Moving the DHT22 sliders changes both within one reading |
| Upload to Render | Batches land in Neon as `wokwi-01`; humidity changes 75 % → 95.5 % recorded; a failed upload was retried and succeeded with no duplicates |
| Offline buffer | Offline with the button, then reconnected: all 30 readings of the run in Neon, 0 duplicates, no gaps over 40 s; the 22 taken from the moment it went offline arrived after reconnecting |

## For the real node (Stage D)

Set `SENSOR` to `SENSOR_SHT31` (install the "Adafruit SHT31 Library"), put node-01's or node-02's key and the 2.4 GHz hotspot in `secrets.h`, and replace `setInsecure()` with the ESP32 core's CA bundle so the server's certificate is checked.
