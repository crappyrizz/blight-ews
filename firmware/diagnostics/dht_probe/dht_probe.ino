// Diagnostic: find which GPIO a DHT22 or DHT11 data wire is really on, and why it fails.
// Tries both sensor types on every safe pin (not the flash pins 6-11, not Serial 1/3,
// not the OLED's 21/22) and prints the library's status for each. Flash the real
// firmware (firmware/blight_node) again afterwards.
#include <DHTesp.h>

const int PINS[] = {15, 2, 4, 5, 13, 14, 16, 17, 18, 19, 23, 25, 26, 27, 32, 33};
DHTesp dht;

void probe(int pin, DHTesp::DHT_MODEL_t model, const char *name) {
  dht.setup(pin, model);
  delay(2100);  // at most one read every 2 s
  TempAndHumidity v = dht.getTempAndHumidity();
  Serial.printf("GPIO %2d %s: %-8s  T=%.1f C  RH=%.1f %%\n", pin, name, dht.getStatusString(), v.temperature,
                v.humidity);
}

void setup() {
  Serial.begin(115200);
  delay(2500);  // sensor warm-up after power-on
}

void loop() {
  Serial.println("--- DHT probe (DHT22 and DHT11) ---");
  for (int pin : PINS) {
    probe(pin, DHTesp::DHT22, "DHT22");
    probe(pin, DHTesp::DHT11, "DHT11");
  }
}
