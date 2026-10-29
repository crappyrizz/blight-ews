// Diagnostic: read the DHT22 on GPIO 15 every 2 s and print the values. No Wi-Fi needed.
// For showing the sensor working on its own. Flash the real firmware (firmware/blight_node) afterwards.
#include <DHTesp.h>

DHTesp dht;
unsigned long n = 0;

void setup() {
  Serial.begin(115200);
  dht.setup(15, DHTesp::DHT22);
  delay(2500);  // warm-up after power-on
  Serial.println("DHT22 sensor check: data on GPIO 15, one reading every 2 s");
}

void loop() {
  TempAndHumidity v = dht.getTempAndHumidity();
  n++;
  if (dht.getStatus() == DHTesp::ERROR_NONE) {
    Serial.printf("reading %lu: temperature %.1f C, humidity %.1f %% RH  (sensor OK)\n", n, v.temperature, v.humidity);
  } else {
    Serial.printf("reading %lu: sensor ERROR (%s)\n", n, dht.getStatusString());
  }
  delay(2000);
}
