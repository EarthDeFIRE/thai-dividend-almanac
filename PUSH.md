# วิธีเอาขึ้น GitHub + Vercel

## 1. สร้าง repo แล้ว push (ทำจากเครื่องคุณ)

```bash
unzip almanac-site.zip -d almanac-site && cd almanac-site
git init -b main
git add .
git commit -m "Thai Dividend Almanac · price & valuation update 22/09/2026 + daily pipeline"
gh repo create earthdefire/thai-dividend-almanac --private --source=. --push
# ถ้าไม่มี gh CLI: สร้าง repo เปล่าบน github.com แล้ว
# git remote add origin https://github.com/<owner>/<repo>.git && git push -u origin main
```

repo จะเป็น private ก็ได้ Vercel ยังดึงไปเดปลอยได้ปกติ ส่วนหน้าเว็บที่เดปลอยแล้วจะเป็น public

## 2. บอกผมสองอย่าง แล้วผมลิงก์ Vercel + เดปลอยให้

- repo: `owner/name`
- Vercel team: slug หรือ team id (ขึ้นต้นด้วย `team_`) ดูได้จาก URL ของ dashboard หรือไฟล์ `.vercel/project.json` ของโปรเจกต์เดิม

ผมจะเรียก Vercel ให้สร้าง project ที่ผูกกับ repo นี้ ทุก push หลังจากนั้นจะ redeploy เอง
รวมถึง commit ที่ cron เขียน `snapshot.json` ด้วย นี่คือเหตุผลที่ต้องผ่าน git ไม่ใช่อัปไฟล์ตรง

## 3. เปิด cron

Actions → workflow `update-snapshot` → Run workflow ดูรอบแรกด้วยตาก่อน
ถ้ารันเขียว `snapshot.json` จะถูก commit และหน้าเว็บจะขึ้นสถานะ 🟢 พร้อมวันที่ของข้อมูล

ก่อนเปิด cron จริง รันในเครื่องหนึ่งรอบ:

```bash
python3 tools/update_snapshot.py --selftest   # ทดสอบ guard ไม่แตะเน็ต
python3 tools/update_snapshot.py --dry-run    # ยิงจริง ดูว่าครบ 24 ตัวไหม
```
