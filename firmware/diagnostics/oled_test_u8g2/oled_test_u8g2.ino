// Diagnostic: drive the OLED (0x3C, SDA 21 / SCL 22) with U8g2, alternating the SSD1306 and SH1106
// drivers every 3 s. Whichever one lights the screen names the controller chip.
// Flash the real firmware (firmware/blight_node) again afterwards.
#include <Wire.h>
#include <U8g2lib.h>

U8G2_SSD1306_128X64_NONAME_F_HW_I2C ssd(U8G2_R0, U8X8_PIN_NONE);
U8G2_SH1106_128X64_NONAME_F_HW_I2C sh(U8G2_R0, U8X8_PIN_NONE);

void show(U8G2 &d, const char *name) {
  d.begin();
  d.setPowerSave(0);
  d.setContrast(255);
  d.clearBuffer();
  d.drawBox(0, 0, 128, 18);
  d.setDrawColor(0);
  d.setFont(u8g2_font_ncenB10_tr);
  d.drawStr(4, 14, name);
  d.setDrawColor(1);
  d.drawStr(4, 44, "driver test");
  d.sendBuffer();
}

void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22);
}

void loop() {
  Serial.println("U8g2 test: now drawing with the SSD1306 driver");
  show(ssd, "SSD1306");
  delay(3000);
  Serial.println("U8g2 test: now drawing with the SH1106 driver");
  show(sh, "SH1106");
  delay(3000);
}
