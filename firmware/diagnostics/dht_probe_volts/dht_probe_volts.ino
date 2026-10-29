// Diagnostic: measure the voltage on the DHT data line (GPIO 15) with the ESP32's ADC.
// With the 10 kOhm pull-up from DATA to the sensor's VCC, and the sensor powered and idle,
// GPIO 15 should sit near 3.3 V. Near 0 V means the sensor's VCC (or the resistor) is not
// connected. Also checks whether the line stays high against the internal pull-down,
// which only a real external pull-up (the resistor) can do.
// Flash the real firmware (firmware/blight_node) again afterwards.

const int DATA_PIN = 15;

void setup() {
  Serial.begin(115200);
  delay(1000);
  analogReadResolution(12);
}

void loop() {
  pinMode(DATA_PIN, INPUT);  // no internal pull-up or pull-down: what the wiring alone gives
  delay(20);
  uint32_t mv = analogReadMilliVolts(DATA_PIN);
  pinMode(DATA_PIN, INPUT_PULLDOWN);  // ~45 kOhm to GND: a 10 kOhm pull-up still wins
  delay(20);
  int withPulldown = digitalRead(DATA_PIN);
  Serial.printf("GPIO 15: %4u mV (floating input); with internal pull-down it reads %s\n", mv,
                withPulldown ? "HIGH (pull-up present)" : "LOW (no pull-up / no power)");
  delay(1000);
}
