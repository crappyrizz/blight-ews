// Diagnostic: list every I2C device answering on SDA 21 / SCL 22 (the OLED should be 0x3C or 0x3D;
// an SHT31 is 0x44). Flash the real firmware (firmware/blight_node) again afterwards.
#include <Wire.h>

void setup() {
  Serial.begin(115200);
  delay(500);
}

void loop() {
  Wire.begin(21, 22);
  int found = 0;
  Serial.print("--- I2C scan (SDA 21, SCL 22):");
  for (uint8_t address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", address);
      found++;
    }
  }
  Serial.printf("  -> %d device(s)\n", found);
  delay(2000);
}
