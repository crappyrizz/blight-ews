// Diagnostic: which GPIO does the push button (or a wire touched to GND) really pull LOW?
// Every listed pin gets the internal pull-up; any change is printed at once, and every 2 s the
// pins currently LOW are listed, so a held touch shows too. The node firmware expects the button
// on GPIO 4 (to GND). Flash the real firmware (firmware/blight_node) afterwards.
const int PINS[] = {4, 15, 2, 0, 5, 12, 13, 14, 16, 17, 18, 19, 23, 25, 26, 27, 32, 33};  // not 21/22 (OLED)
const int N = sizeof(PINS) / sizeof(PINS[0]);
int last[N];

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < N; i++) {
    pinMode(PINS[i], INPUT_PULLUP);
  }
  delay(50);
  for (int i = 0; i < N; i++) {
    last[i] = digitalRead(PINS[i]);
  }
  Serial.println("button check started");
}

void loop() {
  for (int i = 0; i < N; i++) {
    int v = digitalRead(PINS[i]);
    if (v != last[i]) {
      Serial.printf("GPIO %d -> %s\n", PINS[i], v == LOW ? "PRESSED (connected to GND)" : "released");
      last[i] = v;
    }
  }
  static unsigned long beat = 0;
  if (millis() - beat > 2000) {
    beat = millis();
    Serial.print("button check: pins now LOW:");
    for (int i = 0; i < N; i++) {
      if (digitalRead(PINS[i]) == LOW) Serial.printf(" GPIO %d", PINS[i]);
    }
    Serial.println(" (end)");
  }
  delay(20);
}
