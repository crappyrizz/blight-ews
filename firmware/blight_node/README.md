# Sensor node firmware (ESP32)

One Arduino sketch for both the Wokwi simulation and the real node. It measures temperature and humidity, keeps every reading in a buffer saved to flash, and uploads batches to the backend's `POST /readings` (see [docs/firmware_contract.md](../../docs/firmware_contract.md) §4a). The OLED shows the reading, the buffer size, the link status and the risk level the server answers with.

| File | What it is |
|---|---|
| `blight_node.ino` | The sketch. The sensor sits behind `readClimate()`; `SENSOR` picks DHT22 or SHT31. `DISPLAY_CHIP` picks the OLED controller (SSD1306, as in Wokwi and 0.96" modules; SH1106 for 1.3" modules). `DEMO_MODE` picks the intervals. |
| `diagram.json` | The Wokwi circuit: ESP32 DevKit, DHT22 on GPIO 15, SSD1306 OLED on I²C (SDA 21, SCL 22), Offline pushbutton on GPIO 4. |
| `libraries.txt` | Libraries for Wokwi (the same names for arduino-cli / Arduino IDE). |
| `secrets.example.h` | The format of `secrets.h`: Wi-Fi, device ID, device key, API URL. |
| `secrets.h` | **Gitignored.** The real values. |

## Behaviour

| | Demo mode | Normal mode (firmware contract) |
|---|---|---|
| Measure | every 5 s | every 10 min |
| Upload | every 30 s | hourly |

- **No reading without a real clock:** nothing is kept until NTP has set the time (contract §7).
- **Buffer first, send second:** each reading goes into a 7-day ring buffer (1,008 readings), saved to LittleFS after every change and restored at boot. When full, the oldest reading is dropped.
- **Uploads:** batches of up to 100, oldest first, with a 65 s timeout, the most the ESP32 HTTP client can hold (the free Render server takes about a minute to wake; a longer wake-up is caught by the retry). A `200` removes the whole batch from the buffer. `401`, `413` or `422` stops uploads and shows the code on the OLED. Network errors, timeouts and `5xx` keep the data and retry after 1, 2, 4 … 30 min. Reconnecting flushes the buffer at once.
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

## The real node (lab build, 29 Oct 2026)

ESP32 DevKit V1 (CP2102 USB chip: install the Silicon Labs CP210x driver once; it appears as a COM port), a **bare 4-pin DHT22**, and the same sketch with `SENSOR_DHT22`. Registered on the hosted server as **node-01** (owned by the farmer), its key and the 2.4 GHz phone hotspot in `secrets.h`.

| DHT22 leg (grille facing you, left to right) | Goes to |
|---|---|
| 1 VCC | ESP32 **3V3** |
| 2 DATA | ESP32 **D15**, and a **10 kOhm resistor from DATA to VCC** (a bare DHT22 needs it; without it every read times out) |
| 3 | nothing |
| 4 GND | ESP32 **GND** |

Optional, same as Wokwi: OLED on 3V3 / GND / SCL D22 / SDA D21 (the sketch skips it if it does not answer at 0x3C), and the Offline button from D4 to GND on diagonally opposite legs (a button that reads pressed at start-up is ignored until released).

**Tested on the board:** readings every 5 s in demo mode; uploads land in Neon as node-01 with no duplicates; with the hotspot off the node keeps measuring and buffering, and on reconnect delivers the whole backlog once; after a reset only the unsent readings are restored from flash; a sleeping Render server is ridden out by the retry. Not working in the lab build: the OLED (both modules tried answer on I2C but stay dark: about 1.3 V at the module's VCC, a supply/connection fault) and the button (no contact reached the pin); both are shown working in Wokwi.

**Watching it:** `.\.venv\Scripts\python.exe -m tools.watch_readings node-01` prints each reading as it lands in the hosted database, next to the board's serial output (`arduino-cli monitor -p COM8 --config baudrate=115200`).

## Lab diagnostics (`firmware/diagnostics/`)

Small sketches for finding wiring faults; flash one, read the serial output, then flash `blight_node` again.

| Sketch | What it tells you |
|---|---|
| `dht_check` | DHT22 on GPIO 15: temperature and humidity every 2 s, or the library's error |
| `dht_probe` | Tries DHT22 and DHT11 reads on every safe GPIO: finds a data wire on the wrong pin, or a sensor that never answers |
| `dht_probe_volts` | Voltage on the DHT data line (ADC) and whether it holds against the internal pull-down: shows a missing pull-up resistor or an unpowered sensor |
| `i2c_scan` | Every device answering on SDA 21 / SCL 22 (OLED 0x3C or 0x3D, SHT31 0x44) |
| `oled_test_u8g2` | Draws on the OLED with the SSD1306 and SH1106 drivers in turn, to name the controller |
| `button_check` | Which GPIOs read LOW, live: shows where a button (or a wire touched to GND) really connects |
