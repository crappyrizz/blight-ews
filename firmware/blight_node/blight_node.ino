// Blight EWS sensor node: ESP32 + temperature/humidity sensor + SSD1306 OLED.
//
// Stage B of the IoT workplan: LOCAL ONLY. The node measures on a timer,
// shows the reading on the OLED and logs it to the serial monitor. Wi-Fi,
// the NTP clock and the upload to the backend come in Stage C, following
// docs/firmware_contract.md.
//
// Runs unchanged in Wokwi (DHT22, which is the only temperature/humidity part
// Wokwi simulates) and on the real board (SHT31): only readClimate() differs,
// chosen by SENSOR below.
//
// Wiring (Wokwi diagram.json and the real build):
//   DHT22 data -> GPIO 15        (Wokwi / fallback sensor)
//   SHT31 SDA  -> GPIO 21, SCL -> GPIO 22, address 0x44   (real node)
//   OLED  SDA  -> GPIO 21, SCL -> GPIO 22, address 0x3C   (shares the I2C bus)
//   Everything powered from 3V3, never 5 V.

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// --- which sensor ---------------------------------------------------------------------
#define SENSOR_DHT22 1  // Wokwi, or a DHT22 on the real board
#define SENSOR_SHT31 2  // the real node (needs the "Adafruit SHT31 Library")
#define SENSOR SENSOR_DHT22

#if SENSOR == SENSOR_DHT22
#include <DHTesp.h>
#elif SENSOR == SENSOR_SHT31
#include <Adafruit_SHT31.h>
#endif

// --- settings --------------------------------------------------------------------------
const char *DEVICE_ID = "wokwi-01";  // the device's ID on the server (Stage C)

// DEMO_MODE: measure every 15 s so slider changes show quickly. Normal mode:
// every 10 min, the firmware contract's rate. Never leave demo mode running for
// days once uploads are on: it would use up the free hosting allowance.
const bool DEMO_MODE = true;
const unsigned long MEASURE_EVERY_MS = DEMO_MODE ? 15UL * 1000 : 10UL * 60 * 1000;

const uint8_t DHT_PIN = 15;
const uint8_t OLED_ADDRESS = 0x3C;
const uint8_t SHT31_ADDRESS = 0x44;
const int SCREEN_WIDTH = 128;
const int SCREEN_HEIGHT = 64;

// --- one reading ---------------------------------------------------------------------------
struct Climate {
  bool ok;        // false: the sensor did not answer, or answered nonsense
  float tempC;    // degrees C
  float rh;       // relative humidity, %
};

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
bool displayReady = false;

#if SENSOR == SENSOR_DHT22
DHTesp dht;
#elif SENSOR == SENSOR_SHT31
Adafruit_SHT31 sht31;
#endif

unsigned long lastMeasureMs = 0;
unsigned long readingCount = 0;
Climate last = {false, NAN, NAN};

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

// --- the OLED ------------------------------------------------------------------------------------

void showReading(const Climate &c) {
  if (!displayReady) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("Blight EWS ");
  display.print(DEVICE_ID);

  if (c.ok) {
    display.setTextSize(2);
    display.setCursor(0, 14);
    display.print(c.tempC, 1);
    display.print(" C");
    display.setCursor(0, 34);
    display.print(c.rh, 1);
    display.print(" %");
  } else {
    display.setTextSize(1);
    display.setCursor(0, 22);
    display.print("Sensor error:");
    display.setCursor(0, 32);
    display.print("check the wiring");
  }

  display.setTextSize(1);
  display.setCursor(0, 56);
  display.print(DEMO_MODE ? "demo " : "");
  display.print("#");
  display.print(readingCount);
  display.print("  Wi-Fi: off");  // Stage C replaces this with the link status
  display.display();
}

// --- setup and the loop ----------------------------------------------------------------------------

void measure() {
  last = readClimate();
  readingCount++;
  if (last.ok) {
    Serial.printf("reading %lu: %.1f C, %.1f %% RH\n", readingCount, last.tempC, last.rh);
  } else {
    Serial.printf("reading %lu: SENSOR ERROR (not kept)\n", readingCount);
  }
  showReading(last);
}

void setup() {
  Serial.begin(115200);
  Serial.printf("\nBlight EWS node %s, %s mode, measuring every %lu s\n", DEVICE_ID,
                DEMO_MODE ? "demo" : "normal", MEASURE_EVERY_MS / 1000);

  Wire.begin(21, 22);  // SDA, SCL
  displayReady = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS);
  if (!displayReady) Serial.println("OLED not found at 0x3C: continuing without a display");

  if (!setupSensor()) Serial.println("Sensor not found: check the wiring");
  delay(2000);  // a DHT22 needs about 2 s after power-up before its first reading

  measure();
  lastMeasureMs = millis();
}

void loop() {
  // Timers, never delay(): the loop stays free for Wi-Fi and uploads in Stage C.
  if (millis() - lastMeasureMs >= MEASURE_EVERY_MS) {
    lastMeasureMs = millis();
    measure();
  }
}
