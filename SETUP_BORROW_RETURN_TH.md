# Smart Key Box 3 — ยืม–คืนผ่านเว็บไซต์และ ESP32

## เว็บไซต์
- เข้าระบบ Admin ที่ `/` แล้วเลือก **ยืม–คืนกุญแจ**
- เลือกยืมหรือคืน เลือกห้อง 213, 214, 215, 216, 222, 223 และเลือกนักศึกษาที่ลงทะเบียนแล้ว
- ยืนยันเพื่อบันทึก SQLite; ประวัติปรากฏในเมนู **ประวัติการใช้งาน** และหน้า `/status`
- การทำรายการบนเว็บไซต์ถือเป็นการยืนยันโดย Admin ไม่ใช่การสแกนนิ้ว และ **ไม่สั่งเปิดกลอนจริง**
- ระบบใช้เวลาไทย 07:00–19:00 และ Admin สามารถเปิดพิเศษ 30 นาทีได้จากเมนูควบคุมตู้

## ESP32
- ตัวอย่างใน `esp32/SmartKeyBox3_ESP32.ino` ใช้โปรโตคอล Adafruit_Fingerprint UART; ตรวจสอบรุ่นเซ็นเซอร์ก่อนใช้งาน
- กำหนด Wi-Fi, URL ของ Render, DEVICE_API_KEY และใบรับรอง CA จริง
- GPIO16 เป็น RX2 รับจาก TX เซ็นเซอร์; GPIO17 เป็น TX2 ส่งไป RX เซ็นเซอร์; GND ร่วมกัน
- ตรวจสอบแรงดันไฟเลี้ยงและระดับสัญญาณจาก datasheet ของเซ็นเซอร์ **ก่อนต่อสาย**
- ลงทะเบียนแม่แบบลายนิ้วมือไว้บนโมดูลและเชื่อม `fingerprint_id` กับผู้ใช้ในฐานข้อมูลผ่าน Admin API
- สแกนสำเร็จแล้ว ESP32 เรียก `POST /api/borrow` หรือ `POST /api/return` พร้อม `fingerprint_id` และ `key_id`
- ตัวอย่างนี้บันทึกสถานะเท่านั้น **ยังไม่มีการยืนยันว่ากุญแจถูกหยิบ/คืนจริงหรือการสั่งรีเลย์**; หากต้องการใช้กับตู้จริง ควรเพิ่มเซ็นเซอร์ตรวจจับกุญแจและกระบวนการยืนยันการทำรายการก่อนบันทึก

## Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --bind 0.0.0.0:$PORT`
- Environment: `ADMIN_API_KEY`, `DEVICE_API_KEY`, `SECRET_KEY`, `ADMIN_PASSWORD`, `COOKIE_SECURE=1`
- ตั้ง `DATABASE_PATH` ไปยัง persistent disk หรือใช้ฐานข้อมูลภายนอก; Render Free ไม่มี persistent disk และ SQLite อาจหายเมื่อ redeploy
- **เปลี่ยนค่าความลับทั้งหมดที่เคยปรากฏในภาพหน้าจอ** และอย่าอัปโหลดค่าเหล่านั้นขึ้น GitHub

## API
- `POST /web/transactions` ต้องเข้าสู่ระบบ Admin และส่ง CSRF header; body `{ "action":"borrow", "key_id":1, "user_id":1 }`
- `POST /api/borrow` ต้องส่ง `X-API-Key: DEVICE_API_KEY`; body `{ "fingerprint_id":1, "key_id":1 }`
- `POST /api/return` รูปแบบเดียวกับ borrow
- `GET /api/public/status` และ `/status` สำหรับตรวจสอบสถานะสาธารณะ

**หมายเหตุ:** ระบบเว็บในชุดนี้เป็นหน้า Admin สำหรับทำรายการแทนนักศึกษา ไม่ใช่หน้าให้นักศึกษาเข้ามายืมเองโดยไม่ยืนยันตัวตน
