# Smart Key Box 3 — ขั้นที่ 3 ระบบลงทะเบียนลายนิ้วมือผ่านเว็บไซต์

## สิ่งที่เพิ่ม
- ตาราง `enrollment_jobs` สร้างด้วย `CREATE TABLE IF NOT EXISTS` ไม่ลบตารางเดิม
- Admin สร้างงานและยกเลิกงานที่ยัง `pending` ผ่าน Session + CSRF
- ESP32 รับงานด้วย `DEVICE_API_KEY` และส่งผลกลับด้วย API key เดิม
- เมื่อสำเร็จ เซิร์ฟเวอร์ผูก `fingerprint_id` กับนักศึกษาใน SQLite
- หน้า Admin แสดงสถานะรออุปกรณ์ / กำลังสแกน / สำเร็จ / ไม่สำเร็จ
- Firmware คง workflow ยืม–คืนผ่าน Serial Monitor ที่มีอยู่เดิม และเพิ่มการตรวจงาน Enrollment

## สถานะงาน
`pending` → `in_progress` → `success` หรือ `failed`; Admin ยกเลิกได้เฉพาะ `pending`.

## API ใหม่
- `POST /web/enrollments` JSON: `{"user_id": 123}` (Admin Session + `X-CSRF-Token`)
- `GET /web/enrollments/<job_id>` (Admin Session)
- `POST /web/enrollments/<job_id>/cancel` (Admin Session + CSRF)
- `GET /api/device/enrollments/next` (Device API Key)
- `POST /api/device/enrollments/<job_id>/result` (Device API Key), ตัวอย่างผลสำเร็จ: `{"success":true,"fingerprint_id":1,"message":"ลงทะเบียนสำเร็จ"}`

## การ Deploy
1. สำรอง SQLite ก่อนอัปเดต แม้ migration จะเพิ่มตารางอย่างเดียว
2. นำ `app.py` และ `templates/index.html` รุ่นนี้ไปแทนไฟล์เดิม โดยคง `status.html`, `requirements.txt` และตัวแปร Environment เดิม
3. Deploy บน Render แล้วตรวจ `/health` และ `/api/public/status`
4. เข้าสู่ระบบ Admin เพิ่มนักศึกษาทดสอบหนึ่งคน แล้วกดลงทะเบียน
5. เปิด ESP32 และตรวจ Serial Monitor ว่าเชื่อมต่อ Wi-Fi, AS608 และรับงานจากเว็บได้

## ตั้งค่า Firmware
แก้ไขใน `esp32/SmartKeyBox3_ESP32.ino`:
- `WIFI_SSID`, `WIFI_PASSWORD`
- `BASE_URL` เป็น URL Render จริง โดยไม่มี `/` ต่อท้าย
- `DEVICE_KEY` เป็นค่า `DEVICE_API_KEY` เดิม (ห้ามนำไปใส่หน้าเว็บหรือเผยแพร่)
- `ROOT_CA` ต้องเป็น Root CA ที่ตรวจสอบ certificate chain ของโดเมน Render จริง ห้ามใช้ `setInsecure()`

UART ในตัวอย่างนี้ใช้ GPIO16 เป็น RX2 (ต่อกับ TX ของ AS608) และ GPIO17 เป็น TX2 (ต่อกับ RX ของ AS608) ที่ 57600 baud ต้องตรวจสอบ pinout และแรงดันตามรุ่นโมดูลจริงก่อนจ่ายไฟ

## ข้อควรระวัง
- Firmware ใช้ Adafruit Fingerprint Sensor Library และ ArduinoJson 7.x ตามโค้ดนี้
- โค้ดเลือกช่องว่างโดยตรวจ slot 1–127 ใน AS608; อย่าลบหรือบันทึก template ด้วยเครื่องมืออื่นโดยไม่ปรับฐานข้อมูลให้ตรงกัน
- ถ้า AS608 บันทึก template สำเร็จแต่ ESP32 ติดต่อเซิร์ฟเวอร์เพื่อยืนยันผลไม่ได้ จะเกิด template ที่ยังไม่ถูกผูกกับนักศึกษา ให้หยุดลงทะเบียนซ้ำและตรวจสอบ slot ก่อน
- Firmware นี้ไม่ได้ควบคุมกลอนไฟฟ้าโดยตรง; คงการยืม–คืนเดิมผ่าน API และ Serial Monitor เท่านั้น
- ทดสอบกับนักศึกษาทดลองและสำเนาฐานข้อมูลก่อนใช้งานจริง
