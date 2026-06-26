# PAGCOR Game Parameter Bot 🤖

Telegram bot สำหรับสร้างไฟล์ PAGCOR Game Parameters อัตโนมัติ

---

## 📁 โครงสร้างไฟล์

```
pagcor_bot/
├── bot.py              ← โค้ดหลัก
├── requirements.txt    ← Python dependencies
├── railway.toml        ← Railway config
├── .env.example        ← ตัวอย่าง environment variables
└── .gitignore
```

---

## 🚀 วิธี Deploy บน Railway

### ขั้นตอนที่ 1 — เตรียม GitHub Repo

```bash
git init
git add .
git commit -m "first commit"
# สร้าง repo ใหม่บน GitHub แล้ว push
git remote add origin https://github.com/YOUR_USERNAME/pagcor-bot.git
git push -u origin main
```

### ขั้นตอนที่ 2 — สมัคร / Login Railway

ไปที่ [railway.app](https://railway.app) → Login with GitHub

### ขั้นตอนที่ 3 — สร้าง Project ใหม่

1. กด **New Project**
2. เลือก **Deploy from GitHub repo**
3. เลือก repo `pagcor-bot`
4. Railway จะ detect `railway.toml` และ build อัตโนมัติ

### ขั้นตอนที่ 4 — ตั้ง Environment Variables

ไปที่ **Project → Variables** แล้วเพิ่ม 3 ตัวนี้:

| Variable | ค่า |
|---|---|
| `TELEGRAM_TOKEN` | Token จาก @BotFather |
| `ALLOWED_USER_ID` | Telegram User ID ของตัวเอง (ดูได้จาก @userinfobot) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | เนื้อหาทั้งหมดของไฟล์ `service_account.json` (วางเป็น string เดียว) |

> ⚠️ สำหรับ `GOOGLE_SERVICE_ACCOUNT_JSON` ให้เปิดไฟล์ `service_account.json`
> แล้ว copy ทั้งหมดวางใน Railway เลยค่ะ (รวม `{` และ `}` ด้วย)

### ขั้นตอนที่ 5 — Deploy

Railway จะ redeploy อัตโนมัติหลังตั้ง Variables เสร็จค่ะ
ดู log ได้ที่ **Deployments → View Logs**

---

## 💬 วิธีใช้บอท

```
ทำไฟล์เกม Golden Empire, Boxing King, Mega Ace ให้ PY หน่อย
```

บอทจะ:
1. ตรวจสอบชื่อเกมและถามยืนยัน
2. พิมพ์ `ยืนยัน` เพื่อสร้างไฟล์
3. บอทส่ง Google Sheets link กลับมา ✅

---

## 🔍 หา Telegram User ID ของตัวเอง

1. เปิด Telegram → ค้นหา **@userinfobot**
2. กด Start → บอทจะบอก ID ของคุณ

---

## ❓ แก้ปัญหาเบื้องต้น

| ปัญหา | วิธีแก้ |
|---|---|
| Bot ไม่ตอบ | เช็ค `TELEGRAM_TOKEN` ว่าถูกต้อง |
| Google API error | เช็คว่า Service Account มีสิทธิ์ใน Sheets และ Drive |
| `IMPORTRANGE` ไม่มีข้อมูล | ต้องเปิด Sheets ด้วยมือก่อนครั้งแรก แล้ว Allow access |
| Railway หยุดทำงาน | ไปที่ Deployments → Redeploy |
