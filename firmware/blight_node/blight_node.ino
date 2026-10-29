// Blight EWS sensor node: ESP32 + temperature/humidity sensor + 128x64 I2C OLED (SH1106 or SSD1306).
//
// Stage C of the IoT workplan: the node measures on a timer, keeps every
// reading in a buffer that is saved to flash, and uploads it in batches to the backend
// (POST /readings, docs/firmware_contract.md section 4a). The OLED shows the
// latest reading, the link status and the risk level the server answers with.
//
// Runs unchanged in Wokwi (DHT22, the only temperature/humidity part Wokwi
// simulates) and on the real board (SHT31): only readClimate() differs,
// chosen by SENSOR below. Wi-Fi, the device ID and its key live in secrets.h
// (gitignored; secrets.example.h shows the format).
//
// Wiring (Wokwi diagram.json and the real build):
//   DHT22 data -> GPIO 15        (Wokwi / fallback sensor)
//   SHT31 SDA  -> GPIO 21, SCL -> GPIO 22, address 0x44   (real node)
//   OLED  SDA  -> GPIO 21, SCL -> GPIO 22, address 0x3C   (shares the I2C bus)
//   Everything powered from 3V3, never 5 V.
//
// Rules from the firmware contract, and where they are kept:
//   - buffer first, send second ............. measure() stores, uploadDue() sends
//   - delete only what a 200 covers ......... uploadBatch()
//   - 401 / 413 / 422 need a human: stop .... uploadBatch() sets uploadsStopped
//   - never retry faster than once a minute . backoff in uploadBatch()
//   - no reading without a real clock ....... measure() waits for NTP
//   - buffer full: drop the oldest .......... bufferPush()
//   - a power cut loses nothing ............. saveBuffer() / loadBuffer() (LittleFS)
//   - flush on reconnect .................... uploadDue() after the link returns
// The "Offline" pushbutton (GPIO 4) cuts Wi-Fi in code for the demo and the
// buffer test: press to go offline, press again to reconnect.
// Note: Wokwi wipes its simulated flash on every restart, so the power-cut
// part can only be shown on the real board.

#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <LittleFS.h>
#include <U8g2lib.h>

#include "secrets.h"  // WIFI_SSID, WIFI_PASSWORD, DEVICE_ID, DEVICE_KEY, API_URL

// --- which sensor ---------------------------------------------------------------------
#define SENSOR_DHT22 1  // Wokwi, or a DHT22 on the real board
#define SENSOR_SHT31 2  // the real node (needs the "Adafruit SHT31 Library")
#define SENSOR SENSOR_DHT22

// --- which OLED controller ----------------------------------------------------------------
// The lab's 1.3" modules ("1.30 IIC" on the back) use an SH1106; Wokwi's OLED is an SSD1306.
// They look alike and answer at the same address, but each needs its own driver: the wrong one
// leaves the screen dark although the chip answers on I2C.
#define DISPLAY_SSD1306 1
#define DISPLAY_SH1106 2
#define DISPLAY_CHIP DISPLAY_SSD1306  // the 0.96" GME12864-11 module; the 1.3" "1.30 IIC" ones are SH1106

#if SENSOR == SENSOR_DHT22
#include <DHTesp.h>
#elif SENSOR == SENSOR_SHT31
#include <Adafruit_SHT31.h>
#endif

// --- settings --------------------------------------------------------------------------
// DEMO_MODE: measure every 5 s, upload every 30 s, so changes show quickly (e.g. breathing on the sensor).
// Normal mode is the firmware contract's rate: measure every 10 min, upload
// hourly. Never leave demo mode running for days: it would use up the free
// hosting allowance (Render and Neon).
const bool DEMO_MODE = true;
const unsigned long MEASURE_EVERY_MS = DEMO_MODE ? 5UL * 1000 : 10UL * 60 * 1000;  // DHT22: at most one read per 2 s
const unsigned long UPLOAD_EVERY_MS = DEMO_MODE ? 30UL * 1000 : 60UL * 60 * 1000;

// Render can take ~1 min to wake up. HTTPClient keeps this in a uint16_t, so 65,535 ms is the
// most it can hold: 90,000 silently wrapped to ~24.5 s (found on the real board). A wake-up that
// takes longer still is caught by the retry a minute later, by which time the server is up.
const uint16_t HTTP_TIMEOUT_MS = 65000;
const unsigned long RETRY_MIN_MS = 60UL * 1000;        // never retry faster than once a minute
const unsigned long RETRY_MAX_MS = 30UL * 60 * 1000;   // back off to at most 30 min
const size_t BUFFER_SIZE = 1008;                       // 7 days at one reading per 10 min
const size_t BATCH_SIZE = 100;                         // the server takes up to 500

const uint8_t DHT_PIN = 15;
const uint8_t OFFLINE_BUTTON_PIN = 4;  // to GND; pressed = LOW (internal pull-up)
const char *BUFFER_FILE = "/buffer.bin";
const char *BUFFER_TMP = "/buffer.tmp";
const uint8_t OLED_ADDRESS = 0x3C;
const uint8_t SHT31_ADDRESS = 0x44;
const int SCREEN_WIDTH = 128;
const int SCREEN_HEIGHT = 64;
const time_t CLOCK_VALID_AFTER = 1700000000;  // 2023: anything earlier means NTP has not answered yet

// --- one reading ---------------------------------------------------------------------------
struct Climate {
  bool ok;        // false: the sensor did not answer, or answered nonsense
  float tempC;    // degrees C
  float rh;       // relative humidity, %
};

struct Reading {  // what is buffered and sent
  time_t ts;      // UTC, seconds since 1970
  float tempC;
  float rh;
};

#if DISPLAY_CHIP == DISPLAY_SH1106
U8G2_SH1106_128X64_NONAME_F_HW_I2C display(U8G2_R0, U8X8_PIN_NONE);
#else
U8G2_SSD1306_128X64_NONAME_F_HW_I2C display(U8G2_R0, U8X8_PIN_NONE);
#endif
bool displayReady = false;

#if SENSOR == SENSOR_DHT22
DHTesp dht;
#elif SENSOR == SENSOR_SHT31
Adafruit_SHT31 sht31;
#endif

// --- state -------------------------------------------------------------------------------------
Climate last = {false, NAN, NAN};
unsigned long readingCount = 0;
unsigned long lastMeasureMs = 0;
unsigned long lastUploadMs = 0;
unsigned long retryAfterMs = 0;          // 0: no failure pending; else wait until millis() passes it
unsigned long backoffMs = RETRY_MIN_MS;
bool uploadsStopped = false;             // a 401 / 413 / 422: needs a human, not a retry loop
bool forcedOffline = false;              // the Offline button
bool buttonArmed = false;                // set once the button has been seen released
bool storageReady = false;               // LittleFS mounted
bool wasConnected = false;               // to notice the link coming back
unsigned long lastWifiAttemptMs = 0;
String statusLine = "starting";          // shown at the bottom of the OLED
String riskLevel = "-";                  // what the server last answered

// The buffer: a ring of the oldest-to-newest readings not yet confirmed by the server.
Reading buffer[BUFFER_SIZE];
size_t bufferStart = 0;  // index of the oldest reading
size_t bufferCount = 0;

void bufferPush(const Reading &r) {
  if (bufferCount == BUFFER_SIZE) {  // full: drop the OLDEST (recent weather matters more)
    bufferStart = (bufferStart + 1) % BUFFER_SIZE;
    bufferCount--;
  }
  buffer[(bufferStart + bufferCount) % BUFFER_SIZE] = r;
  bufferCount++;
}

const Reading &bufferAt(size_t i) { return buffer[(bufferStart + i) % BUFFER_SIZE]; }

void bufferDropOldest(size_t n) {
  n = min(n, bufferCount);
  bufferStart = (bufferStart + n) % BUFFER_SIZE;
  bufferCount -= n;
}

// The buffer in flash: rewritten after every change (a few KB at most), first
// to a temporary file and then renamed over the old one, so a power cut in the
// middle of a write leaves the previous copy intact.
void saveBuffer() {
  if (!storageReady) return;
  File f = LittleFS.open(BUFFER_TMP, "w");
  if (!f) {
    Serial.println("flash: cannot write the buffer");
    return;
  }
  for (size_t i = 0; i < bufferCount; i++) {
    const Reading &r = bufferAt(i);
    f.write((const uint8_t *)&r, sizeof(Reading));
  }
  f.close();
  bool renamed = LittleFS.rename(BUFFER_TMP, BUFFER_FILE);
  if (!renamed) {
    LittleFS.remove(BUFFER_FILE);
    renamed = LittleFS.rename(BUFFER_TMP, BUFFER_FILE);
  }
  File check = LittleFS.open(BUFFER_FILE, "r");
  size_t bytes = check ? check.size() : 0;
  if (check) check.close();
  if (!renamed || bytes != bufferCount * sizeof(Reading)) {
    Serial.printf("flash: SAVE PROBLEM: %u readings in memory, %u bytes in %s (expected %u), rename %s\n",
                  (unsigned)bufferCount, (unsigned)bytes, BUFFER_FILE, (unsigned)(bufferCount * sizeof(Reading)),
                  renamed ? "ok" : "FAILED");
  } else if (DEMO_MODE) {
    Serial.printf("flash: saved %u readings (%u bytes)\n", (unsigned)bufferCount, (unsigned)bytes);
  }
}

void loadBuffer() {
  File f = LittleFS.open(BUFFER_FILE, "r");
  if (!f) return;
  Reading r;
  size_t loaded = 0, skipped = 0;
  while (f.read((uint8_t *)&r, sizeof(Reading)) == sizeof(Reading)) {
    // Keep only plausible rows: a damaged file must never send nonsense.
    bool ok = r.ts > CLOCK_VALID_AFTER && r.tempC >= -40 && r.tempC <= 85 && r.rh >= 0 && r.rh <= 100;
    if (ok) {
      bufferPush(r);
      loaded++;
    } else {
      skipped++;
    }
  }
  f.close();
  Serial.printf("flash: %u buffered readings restored (%u damaged rows skipped)\n", (unsigned)loaded,
                (unsigned)skipped);
}

// --- the sensor: THE ONLY PART THAT CHANGES BETWEEN WOKWI AND THE REAL NODE ----------------

bool setupSensor() {
#if SENSOR == SENSOR_DHT22
  dht.setup(DHT_PIN, DHTesp::DHT22);
  return true;
#elif SENSOR == SENSOR_SHT31
  return sht31.begin(SHT31_ADDRESS);
#endif
}

Climate readClimate() {
  Climate c = {false, NAN, NAN};
#if SENSOR == SENSOR_DHT22
  TempAndHumidity v = dht.getTempAndHumidity();
  if (dht.getStatus() != DHTesp::ERROR_NONE) return c;
  c.tempC = v.temperature;
  c.rh = v.humidity;
#elif SENSOR == SENSOR_SHT31
  c.tempC = sht31.readTemperature();
  c.rh = sht31.readHumidity();
#endif
  // The same limits the server applies (firmware contract): outside them the
  // sensor is faulty, so the reading is never kept or sent.
  c.ok = !isnan(c.tempC) && !isnan(c.rh) && c.tempC >= -40 && c.tempC <= 85 && c.rh >= 0 && c.rh <= 100;
  return c;
}

// --- the clock -------------------------------------------------------------------------------------

bool clockIsSet() { return time(nullptr) > CLOCK_VALID_AFTER; }

String isoUtc(time_t ts) {  // 2026-10-26T09:15:00Z, as the server expects
  struct tm t;
  gmtime_r(&ts, &t);
  char text[25];
  strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &t);
  return String(text);
}

// --- the OLED ------------------------------------------------------------------------------------

void showScreen() {
  if (!displayReady) return;
  // U8g2: setCursor gives the text's BASELINE, so each line's y is the bottom of its letters.
  display.clearBuffer();

  display.setFont(u8g2_font_6x10_tf);
  display.setCursor(0, 9);
  display.print(DEVICE_ID);
  display.print(DEMO_MODE ? " demo" : "");
  display.setCursor(80, 9);
  display.print("buf ");
  display.print(bufferCount);

  if (last.ok) {
    display.setFont(u8g2_font_ncenB14_tr);
    display.setCursor(0, 27);
    display.print(last.tempC, 1);
    display.print(" C");
    display.setCursor(0, 44);
    display.print(last.rh, 1);
    display.print(" %");
  } else {
    display.setCursor(0, 26);
    display.print(clockIsSet() ? "Sensor error:" : "Waiting for the");
    display.setCursor(0, 37);
    display.print(clockIsSet() ? "check the wiring" : "clock (NTP)...");
  }

  display.setFont(u8g2_font_6x10_tf);
  display.setCursor(0, 54);
  display.print("Risk: ");
  display.print(riskLevel);
  display.setCursor(0, 64);
  display.print(statusLine);
  display.sendBuffer();
}

// --- Wi-Fi ------------------------------------------------------------------------------------------

void keepWifiUp() {
  if (forcedOffline || WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiAttemptMs < 30UL * 1000 && lastWifiAttemptMs != 0) return;  // try every 30 s
  lastWifiAttemptMs = millis();
  Serial.println("Wi-Fi: connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD, 6);  // channel 6 makes Wokwi-GUEST connect faster
}

// The Offline button: press to cut Wi-Fi, press again to reconnect.
void checkOfflineButton() {
  static int lastState = HIGH;
  static unsigned long lastChangeMs = 0;
  int state = digitalRead(OFFLINE_BUTTON_PIN);
  if (!buttonArmed) {
    if (state == HIGH) {
      buttonArmed = true;  // released at last: from now on a press counts
      Serial.println("Offline button: released, now active");
    }
    return;
  }
  if (state == lastState || millis() - lastChangeMs < 50) return;  // 50 ms debounce
  lastChangeMs = millis();
  lastState = state;
  if (state != LOW) return;  // act on the press, not the release
  forcedOffline = !forcedOffline;
  if (forcedOffline) {
    WiFi.disconnect();
    statusLine = "OFFLINE (button)";
    Serial.println("Offline button: Wi-Fi off, readings are buffered");
  } else {
    statusLine = "reconnecting";
    lastWifiAttemptMs = 0;  // reconnect straight away
    Serial.println("Offline button: reconnecting");
  }
}

// --- measuring --------------------------------------------------------------------------------------

void measure() {
  last = readClimate();
  if (!clockIsSet()) {
    // Contract section 7: never invent timestamps. Show the reading, keep nothing.
    Serial.println("reading not kept: the clock is not set yet");
    last.ok = false;
    return;
  }
  readingCount++;
  if (last.ok) {
    bufferPush({time(nullptr), last.tempC, last.rh});
    saveBuffer();  // buffer first, send second
    Serial.printf("reading %lu: %.1f C, %.1f %% RH at %s (buffered: %u)\n", readingCount, last.tempC, last.rh,
                  isoUtc(time(nullptr)).c_str(), (unsigned)bufferCount);
  } else {
    Serial.printf("reading %lu: SENSOR ERROR (not kept)\n", readingCount);
  }
}

// --- uploading --------------------------------------------------------------------------------------

// Send the oldest readings (at most BATCH_SIZE). Returns true if the server took them.
bool uploadBatch() {
  size_t n = min(bufferCount, BATCH_SIZE);
  JsonDocument body;
  JsonArray rows = body.to<JsonArray>();
  for (size_t i = 0; i < n; i++) {
    const Reading &r = bufferAt(i);
    JsonObject row = rows.add<JsonObject>();
    row["device_id"] = DEVICE_ID;
    row["ts"] = isoUtc(r.ts);
    row["temp_c"] = round(r.tempC * 10) / 10.0;
    row["rh"] = round(r.rh * 10) / 10.0;
  }
  String payload;
  serializeJson(body, payload);

  WiFiClientSecure tls;
  tls.setInsecure();  // Wokwi only. The real node verifies the certificate (Stage D: CA bundle).
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(30000);
  if (!http.begin(tls, API_URL)) {
    statusLine = "bad API_URL";
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);

  statusLine = "sending " + String(n) + "...";
  showScreen();
  Serial.printf("upload: %u readings...\n", (unsigned)n);
  int code = http.POST(payload);
  String reply = http.getString();
  http.end();

  if (code == 200) {
    // A 200 means the server is done with every row in the batch: stored,
    // already had it, or rejected it. All of them leave the buffer.
    JsonDocument answer;
    if (deserializeJson(answer, reply) == DeserializationError::Ok) {
      riskLevel = answer["risk_level"] | "?";
      Serial.printf("upload: 200, stored %d, skipped %d, rejected %u, risk %s\n", (int)(answer["stored"] | 0),
                    (int)(answer["skipped"] | 0), (unsigned)answer["rejected"].size(), riskLevel.c_str());
    }
    bufferDropOldest(n);
    saveBuffer();
    backoffMs = RETRY_MIN_MS;
    retryAfterMs = 0;
    statusLine = "online, sent " + String(n);
    return true;
  }

  if (code == 401 || code == 413 || code == 422) {
    // Contract section 5: these need a human. Keep the readings, stop sending.
    uploadsStopped = true;
    statusLine = "STOPPED: HTTP " + String(code);
    Serial.printf("upload: HTTP %d, uploads stopped (check DEVICE_ID / DEVICE_KEY): %s\n", code, reply.c_str());
    return false;
  }

  // Network trouble, timeout or a 5xx: keep everything and try again later,
  // waiting longer each time (1, 2, 4 ... 30 min).
  retryAfterMs = millis() + backoffMs;
  statusLine = "retry in " + String(backoffMs / 60000) + " min";
  Serial.printf("upload: failed (%d %s), retrying in %lu s\n", code, http.errorToString(code).c_str(),
                backoffMs / 1000);
  backoffMs = min(backoffMs * 2, RETRY_MAX_MS);
  return false;
}

void uploadDue() {
  bool connected = WiFi.status() == WL_CONNECTED && !forcedOffline;
  bool justReconnected = connected && !wasConnected;
  wasConnected = connected;
  if (uploadsStopped || bufferCount == 0 || !connected || !clockIsSet()) return;
  if (justReconnected && retryAfterMs == 0) lastUploadMs = millis() - UPLOAD_EVERY_MS;  // flush on reconnect
  if (retryAfterMs != 0 && (long)(millis() - retryAfterMs) < 0) return;  // still backing off
  if (retryAfterMs == 0 && millis() - lastUploadMs < UPLOAD_EVERY_MS) return;
  lastUploadMs = millis();
  // One batch at a time; a long backlog goes out batch after batch.
  while (bufferCount > 0 && uploadBatch()) {
  }
}

// --- setup and the loop ----------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  Serial.printf("\nBlight EWS node %s, %s mode: measure every %lu s, upload every %lu s\n", DEVICE_ID,
                DEMO_MODE ? "demo" : "normal", MEASURE_EVERY_MS / 1000, UPLOAD_EVERY_MS / 1000);

  Wire.begin(21, 22);  // SDA, SCL
  // Only drive the OLED if it answers: with no display fitted, every frame would fail and flood
  // the serial log with I2C errors.
  Wire.beginTransmission(OLED_ADDRESS);
  if (Wire.endTransmission() == 0) {
    displayReady = display.begin();  // U8g2 uses address 0x3C by default
    display.setContrast(255);
  }
  if (!displayReady) Serial.println("OLED not found at 0x3C: continuing without a display");
  if (!setupSensor()) Serial.println("Sensor not found: check the wiring");

  pinMode(OFFLINE_BUTTON_PIN, INPUT_PULLUP);
  delay(10);
  buttonArmed = digitalRead(OFFLINE_BUTTON_PIN) == HIGH;
  if (!buttonArmed) {
    Serial.println("Offline button reads PRESSED at start-up: ignored until released "
                   "(if it never releases, its wires are on the same pair of legs: use diagonal legs)");
  }
  storageReady = LittleFS.begin(true);  // true: format the partition the first time
  if (storageReady) {
    loadBuffer();
  } else {
    Serial.println("flash: LittleFS unavailable, buffering in RAM only");
  }

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  keepWifiUp();
  configTime(0, 0, "pool.ntp.org", "time.google.com");  // UTC; the server converts to Nairobi time
  showScreen();

  delay(2000);  // a DHT22 needs about 2 s after power-up before its first reading
  lastMeasureMs = millis() - MEASURE_EVERY_MS;  // measure straight away
  lastUploadMs = millis();
}

void loop() {
  checkOfflineButton();
  keepWifiUp();
  bool connected = WiFi.status() == WL_CONNECTED && !forcedOffline;
  if (connected && (statusLine == "starting" || statusLine == "offline, buffering" || statusLine == "reconnecting")) {
    statusLine = "Wi-Fi ok";
  }
  if (!connected && !uploadsStopped) statusLine = forcedOffline ? "OFFLINE (button)" : "offline, buffering";

  if (millis() - lastMeasureMs >= MEASURE_EVERY_MS) {
    lastMeasureMs = millis();
    measure();
    showScreen();
  }
  uploadDue();  // blocks while a request is in flight (up to 65 s); the next reading follows it
  showScreen();
  delay(200);  // keep the OLED from redrawing hundreds of times a second
}
