/*
 * ESP32 + UART fingerprint sensor example, compatible with Adafruit_Fingerprint
 * protocol only. Confirm your sensor model, pinout, voltage and UART levels first.
 * No physical lock control is implemented here.
 * Arduino libraries: Adafruit Fingerprint Sensor Library, ArduinoJson.
 * Enrollment: enroll templates on the sensor and associate template ID with
 * a user using authenticated /api/users/<id> PATCH (ADMIN_API_KEY).
 */
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_Fingerprint.h>

const char* WIFI_SSID = "CHANGE_WIFI";
const char* WIFI_PASSWORD = "CHANGE_PASSWORD";
const char* BASE_URL = "https://YOUR-RENDER-SERVICE.onrender.com";
const char* DEVICE_KEY = "CHANGE_DEVICE_API_KEY";
// Replace with the PEM certificate of the CA that validates your Render domain.
// Do not use setInsecure() for an actual cabinet system.
const char* ROOT_CA = R"EOF(-----BEGIN CERTIFICATE-----
REPLACE_WITH_TRUSTED_ROOT_CA_CERTIFICATE
-----END CERTIFICATE-----)EOF";

HardwareSerial sensorSerial(2);
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&sensorSerial);

bool postJSON(const char* route, int fpId, int keyId) {
  WiFiClientSecure client;
  client.setCACert(ROOT_CA);
  HTTPClient http;
  String url = String(BASE_URL) + route;
  if (!http.begin(client, url)) return false;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_KEY);
  JsonDocument payload;
  payload["fingerprint_id"] = fpId;
  payload["key_id"] = keyId;
  String json;
  serializeJson(payload, json);
  int code = http.POST(json);
  String response = http.getString();
  Serial.printf("HTTP %d: %s\n", code, response.c_str());
  http.end();
  return code == 201;
}

int scanFinger() {
  if (finger.getImage() != FINGERPRINT_OK) return -1;
  if (finger.image2Tz() != FINGERPRINT_OK) return -1;
  if (finger.fingerFastSearch() != FINGERPRINT_OK) return -1;
  return finger.fingerID;
}

void setup() {
  Serial.begin(115200);
  // GPIO16=RX2 (sensor TX), GPIO17=TX2 (sensor RX)
  sensorSerial.begin(57600, SERIAL_8N1, 16, 17);
  finger.begin(57600);
  if (!finger.verifyPassword()) {
    Serial.println("Fingerprint sensor not detected / incompatible protocol");
    while (true) delay(1000);
  }
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  Serial.println("Select 1-6 and b=borrow/r=return in Serial Monitor; scan fingerprint");
}

void loop() {
  static int selectedKey = 1;
  static char action = 'b';
  if (Serial.available()) {
    char c = Serial.read();
    if (c >= '1' && c <= '6') selectedKey = c - '0';
    if (c == 'b' || c == 'r') action = c;
    Serial.printf("Selected key %d, action %c\n", selectedKey, action);
  }
  int id = scanFinger();
  if (id > 0 && WiFi.status() == WL_CONNECTED) {
    bool ok = postJSON(action == 'b' ? "/api/borrow" : "/api/return", id, selectedKey);
    Serial.println(ok ? "Recorded in SQLite" : "Denied / network error");
    delay(2500);
  }
  delay(120);
}
