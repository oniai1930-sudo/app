# Smart Key Box 3 — เว็บ Flask + SQLite พร้อมเตรียมเชื่อม ESP32

## ฟังก์ชันที่พร้อมใช้
- หน้า `/` เข้าสู่ระบบ Admin ด้วยรหัสผ่านจาก environment (ไม่มีรหัสผ่านฝังใน HTML)
- หน้า `/status` สาธารณะ อ่านอย่างเดียว รีเฟรชทุก 5 วินาที ไม่แสดงข้อมูลนักศึกษา
- จัดการนักศึกษา (เพิ่ม, เปิด/ปิดบัญชี), ดูสถานะกุญแจ 6 ช่อง, ดูประวัติยืมคืน
- Admin ควบคุมตู้ 07:00–19:00 (เวลาไทย), เปิดพิเศษ 30 นาที, ปิดตู้, บันทึก audit ลง SQLite
- API อุปกรณ์เดิมยังใช้ X-API-Key แยกจาก Admin

## ติดตั้งบน Windows PowerShell
1. ติดตั้ง Python 3.11+ และเปิด PowerShell ในโฟลเดอร์นี้
2. `py -m pip install -r requirements.txt`
3. กำหนดค่า environment (สร้างค่าลับใหม่ของตัวเอง ห้ามนำตัวอย่างไปใช้จริง):
   `$env:ADMIN_API_KEY = 'ใส่คีย์สุ่มยาวของคุณ'`
   `$env:DEVICE_API_KEY = 'ใส่คีย์สุ่มอีกค่าที่ต่างกัน'`
   `$env:ADMIN_PASSWORD = 'ตั้งรหัสผ่านแอดมินที่คาดเดายาก'`
   `$env:SECRET_KEY = 'ใส่คีย์สุ่มสำหรับ session'`
   `$env:COOKIE_SECURE = '0'`  (เฉพาะทดสอบ localhost ผ่าน HTTP)
4. `py app.py`
5. เปิด `http://127.0.0.1:5000/` และ `http://127.0.0.1:5000/status`

## Deploy บน Render
- สร้าง Python Web Service ใหม่ (ห้ามนำ ZIP นี้ไปทับ Node/Express service เดิมโดยไม่ย้ายข้อมูล)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app`
- Environment: `ADMIN_API_KEY`, `DEVICE_API_KEY`, `ADMIN_PASSWORD`, `SECRET_KEY` เป็นค่าลับสุ่มที่ต่างกัน; `COOKIE_SECURE=1`
- SQLite ต้องอยู่บน Persistent Disk และตั้ง `DATABASE_PATH=/var/data/fingerprints.db` ให้ตรง mount path
- Render Free ไม่มี persistent disk แบบเดียวกับบริการเสียเงิน ข้อมูล SQLite อาจหายหลัง deploy/restart หากไม่มี disk
- ใช้ HTTPS เท่านั้นในระบบออนไลน์ และจำกัดสิทธิ์ API key ไม่เปิดเผยใน HTML/GitHub

## ข้อจำกัดก่อนใช้งานกับตู้จริง
- ยังไม่ได้ซื้อ ESP32/เซนเซอร์ลายนิ้วมือ จึงยังลงทะเบียนลายนิ้วมือและเปิดกลอนไฟฟ้าไม่ได้
- หน้าเว็บไม่อนุญาตให้ยืม/คืนด้วยการกดปุ่มโดยไม่มีลายนิ้วมือ
- การจับคู่ fingerprint_id ต้องมาจากขั้นตอน enrollment ของอุปกรณ์จริง ไม่ใช่เดาค่ารหัสลายนิ้วมือ
- API `/api/device/authorize` ต้องถูกตรวจสอบก่อนปลดล็อกทุกครั้ง อุปกรณ์ต้อง fail closed เมื่อ API ไม่ตอบสนอง
- API `/api/verify` เป็นการตรวจสอบตัวตนที่ลงทะเบียน ไม่ใช่คำอนุญาตให้เปิดตู้
- ทดสอบการเข้าถึง การป้องกัน brute force การปิดตู้ การคืนกุญแจ การรีสตาร์ต และไฟดับก่อนใช้จริง
- หากใช้งานกับข้อมูลนักศึกษา ควรแจ้งวัตถุประสงค์และกำหนดสิทธิ์เข้าถึงข้อมูลตามนโยบายสถานศึกษา

## ข้อจำกัดการย้ายระบบ
ไฟล์นี้เป็น Flask + SQLite คนละ backend กับ Node/Express เดิม หากมีฐานข้อมูลเดิมต้องสำรองและทำ migration แยกต่างหาก ไม่ควรชี้ไฟล์ DB ข้าม schema โดยตรง
