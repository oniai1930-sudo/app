/*
 * Smart Key Box 3 - ESP32 + AS608 web enrollment and fingerprint lookup
 * Libraries: Adafruit Fingerprint Sensor Library, ArduinoJson 7.x
 * UART2: GPIO16 (ESP32 RX2 <- AS608 TX), GPIO17 (ESP32 TX2 -> AS608 RX)
 * Confirm the actual sensor pin labels and logic voltage before powering it.
 * The server stores only the AS608 template slot number, not biometric data.
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
// Paste the trusted CA certificate chain root that validates your Render domain.
// Do not use setInsecure() in the deployed cabinet.
const char* ROOT_CA = R"EOF(-----BEGIN CERTIFICATE-----
PASTE_TRUSTED_ROOT_CA_CERTIFICATE_HERE
-----END CERTIFICATE-----)EOF";

HardwareSerial sensorSerial(2);
Adafruit_Fingerprint finger(&sensorSerial);
unsigned long lastPoll = 0;
const unsigned long POLL_INTERVAL_MS = 3000;

bool httpRequest(const String& route, const String& method, const String& body,
                 String& response, int& statusCode) {
  WiFiClientSecure client;
  client.setCACert(ROOT_CA);
  HTTPClient http;
  String url = String(BASE_URL) + route;
  if (!http.begin(client, url)) return false;
  http.setTimeout(12000);
  http.addHeader("X-API-Key", DEVICE_KEY);
  if (method == "POST") http.addHeader("Content-Type", "application/json");
  statusCode = (method == "GET") ? http.GET() : http.POST(body);
  response = statusCode > 0 ? http.getString() : "";
  http.end();
  return statusCode > 0;
}

void sendEnrollmentResult(int jobId, bool success, int fpId, const String& message) {
  JsonDocument payload;
  payload["success"] = success;
  if (success) payload["fingerprint_id"] = fpId;
  payload["message"] = message;
  String body;
  serializeJson(payload, body);
  String response;
  int code = 0;
  String route = "/api/device/enrollments/" + String(jobId) + "/result";
  bool sent = false;
  // Retry the result post to reduce the chance of an orphaned sensor template.
  for (int attempt = 1; attempt <= 3 && !sent; attempt++) {
    sent = httpRequest(route, "POST", body, response, code) && code >= 200 && code < 300;
    if (!sent) delay(1500);
  }
  Serial.printf("Enrollment result HTTP %d; acknowledged=%s; %s\n", code,
                sent ? "yes" : "no", response.c_str());
  if (!sent && success) {
    Serial.println("WARNING: Template is stored in AS608 but server did not confirm mapping.");
    Serial.println("Do not enroll this student again until the slot is reconciled by Admin.");
  }
}

bool waitForFingerImage(uint32_t timeoutMs, const char* prompt) {
  Serial.println(prompt);
  uint32_t start = millis();
  while (millis() - start < timeoutMs) {
    uint8_t p = finger.getImage();
    if (p == FINGERPRINT_OK) return true;
    if (p != FINGERPRINT_NOFINGER && p != FINGERPRINT_PACKETRECIEVEERR) {
      Serial.printf("Sensor image error: %u\n", p);
    }
    delay(100);
  }
  return false;
}

bool waitFingerRemoved(uint32_t timeoutMs) {
  Serial.println("ยกนิ้วออกจากเซนเซอร์");
  uint32_t start = millis();
  while (millis() - start < timeoutMs) {
    if (finger.getImage() == FINGERPRINT_NOFINGER) return true;
    delay(100);
  }
  return false;
}

// Find the first slot that is empty on the sensor. This checks actual sensor
// occupancy rather than assuming that templateCount+1 is always free.
int findFreeSlot() {
  for (uint16_t id = 1; id <= 127; id++) {
    uint8_t p = finger.loadModel(id);
    if (p == FINGERPRINT_NOTFOUND) return id;
    if (p == FINGERPRINT_OK) continue;
    Serial.printf("Cannot inspect template slot %u; sensor error %u\n", id, p);
    return -1;
  }
  return -1;
}

bool enrollOnSensor(int slot) {
  if (!waitForFingerImage(30000, "วางนิ้วครั้งที่ 1 ภายใน 30 วินาที")) {
    Serial.println("หมดเวลารอสแกนนิ้วครั้งแรก");
    return false;
  }
  uint8_t p = finger.image2Tz(1);
  if (p != FINGERPRINT_OK) {
    Serial.printf("แปลงภาพครั้งที่ 1 ไม่สำเร็จ: %u\n", p);
    return false;
  }
  if (!waitFingerRemoved(15000)) {
    Serial.println("ไม่ได้ยกนิ้วออกภายในเวลาที่กำหนด");
    return false;
  }
  if (!waitForFingerImage(30000, "วางนิ้วเดิมครั้งที่ 2")) {
    Serial.println("หมดเวลารอสแกนนิ้วครั้งที่ 2");
    return false;
  }
  p = finger.image2Tz(2);
  if (p != FINGERPRINT_OK) {
    Serial.printf("แปลงภาพครั้งที่ 2 ไม่สำเร็จ: %u\n", p);
    return false;
  }
  p = finger.createModel();
  if (p != FINGERPRINT_OK) {
    Serial.printf("ลายนิ้วมือสองครั้งไม่ตรงกันหรือสร้างโมเดลไม่สำเร็จ: %u\n", p);
    return false;
  }
  p = finger.storeModel(slot);
  if (p != FINGERPRINT_OK) {
    Serial.printf("บันทึกลง AS608 ไม่สำเร็จ: %u\n", p);
    return false;
  }
  return true;
}

void pollEnrollment() {
  String response;
  int code = 0;
  if (!httpRequest("/api/device/enrollments/next", "GET", "", response, code)) {
    Serial.println("ติดต่อเซิร์ฟเวอร์เพื่อตรวจงานลงทะเบียนไม่สำเร็จ");
    return;
  }
  if (code == 204) return;
  if (code != 200) {
    Serial.printf("Enrollment poll HTTP %d: %s\n", code, response.c_str());
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, response)) {
    Serial.println("อ่าน JSON งานลงทะเบียนไม่สำเร็จ");
    return;
  }
  if (doc["busy"] == true) return;
  int jobId = doc["job_id"] | 0;
  const char* fullName = doc["full_name"] | "นักศึกษา";
  const char* studentId = doc["student_id"] | "";
  if (jobId <= 0) return;

  Serial.println("\n===== มีงานลงทะเบียนลายนิ้วมือ =====");
  Serial.printf("นักศึกษา: %s (%s) | Job: %d\n", fullName, studentId, jobId);
  int slot = findFreeSlot();
  if (slot < 1) {
    sendEnrollmentResult(jobId, false, 0, "ไม่พบช่องว่างใน AS608 หรืออ่านหน่วยความจำเซนเซอร์ไม่ได้");
    return;
  }
  Serial.printf("ช่อง Fingerprint ID ที่เลือก: %d\n", slot);
  bool ok = enrollOnSensor(slot);
  if (ok) {
    Serial.printf("บันทึกแม่แบบใน AS608 สำเร็จ: ID %d\n", slot);
    sendEnrollmentResult(jobId, true, slot, "ลงทะเบียนลายนิ้วมือสำเร็จ");
  } else {
    sendEnrollmentResult(jobId, false, 0, "ลงทะเบียนไม่สำเร็จ กรุณาตรวจสอบการวางนิ้วและลองใหม่");
  }
  Serial.println("===== จบงานลงทะเบียน =====\n");
}


bool postTransaction(const char* route, int fpId, int keyId) {
  JsonDocument payload;
  payload["fingerprint_id"] = fpId;
  payload["key_id"] = keyId;
  String body;
  serializeJson(payload, body);
  String response;
  int code = 0;
  bool ok = httpRequest(route, "POST", body, response, code) && code == 201;
  Serial.printf("Transaction HTTP %d: %s\n", code, response.c_str());
  return ok;
}

int scanRegisteredFinger() {
  if (finger.getImage() != FINGERPRINT_OK) return -1;
  if (finger.image2Tz() != FINGERPRINT_OK) return -1;
  if (finger.fingerFastSearch() != FINGERPRINT_OK) return -1;
  return finger.fingerID;
}

void setup() {
  Serial.begin(115200);
  sensorSerial.begin(57600, SERIAL_8N1, 16, 17); // RX2, TX2
  finger.begin(57600);
  if (!finger.verifyPassword()) {
    Serial.println("ไม่พบ AS608 หรือรหัสผ่านเซนเซอร์ไม่ถูกต้อง ตรวจสาย TX/RX และไฟเลี้ยง");
    while (true) delay(1000);
  }
  Serial.printf("AS608 พร้อมใช้งาน; template count=%u\n", finger.templateCount);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("กำลังเชื่อมต่อ Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print('.'); }
  Serial.printf("\nWi-Fi connected: %s\n", WiFi.localIP().toString().c_str());
  Serial.println("พร้อมรับคำสั่งลงทะเบียนจากเว็บไซต์ Admin");
}

void loop() {
  static int selectedKey = 1;
  static char action = 'b';
  static unsigned long lastTransaction = 0;
  if (Serial.available()) {
    char c = Serial.read();
    if (c >= '1' && c <= '6') selectedKey = c - '0';
    if (c == 'b' || c == 'r') action = c;
    Serial.printf("เลือกช่อง %d; รายการ %s\n", selectedKey, action == 'b' ? "ยืม" : "คืน");
  }
  if (WiFi.status() == WL_CONNECTED && millis() - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = millis();
    pollEnrollment();
  }
  // Retain the existing Serial Monitor borrow/return workflow.
  if (WiFi.status() == WL_CONNECTED && millis() - lastTransaction > 2500) {
    int fp = scanRegisteredFinger();
    if (fp > 0) {
      bool ok = postTransaction(action == 'b' ? "/api/borrow" : "/api/return", fp, selectedKey);
      Serial.println(ok ? "บันทึกรายการยืม/คืนแล้ว" : "รายการถูกปฏิเสธหรือเครือข่ายผิดพลาด");
      lastTransaction = millis();
    }
  }
  delay(100);
}
