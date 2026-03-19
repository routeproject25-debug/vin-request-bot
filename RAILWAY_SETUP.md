# 🚂 Налаштування Railway для Google Sheets

Цей файл актуалізовано під поточну логіку бота:
- одна загальна заявка в Telegram-чаті,
- split-експорт у Google Sheets для кількох НП розвантаження,
- дочірні ID рядків у таблиці (`A1B2C3D4-01`, `A1B2C3D4-02`, ...).

## Змінні середовища на Railway

Додайте ці змінні в Railway Dashboard → Variables:

### 1. Основні змінні (вже є)
```
TELEGRAM_BOT_TOKEN=ваш_токен
TARGET_CHAT_ID=ваш_chat_id
BOT_USERNAME=ваш_username
NOVAPOSHTA_API_KEY=ваш_api_key
DATABASE_URL=postgresql://... (автоматично створюється Railway)

# Опційно - для сповіщень адміна про помилки експорту
ADMIN_USER_ID=ваш_telegram_user_id
```

### 2. Google Sheets zmінні (ДОДАТИ)

#### GOOGLE_CREDENTIALS_JSON
**Вміст:** Весь JSON з вашого Service Account файлу одним рядком

**Як додати:**
1. Відкрити ваш Service Account JSON файл (завантажений з Google Cloud Console)
2. Скопіювати **ВЕСЬ** вміст (від `{` до `}`)
3. Видалити всі переноси рядків (зробити один рядок)
4. Вставити в Railway як значення змінної `GOOGLE_CREDENTIALS_JSON`

**Приклад (скорочено):**
```json
{"type":"service_account","project_id":"your-project-id","private_key_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\n...","client_email":"your-service-account@your-project.iam.gserviceaccount.com"}
```

#### GOOGLE_SPREADSHEET_ID
```
your_spreadsheet_id_from_url
```

#### GOOGLE_WORKSHEET_NAME
```
ЗАЯВКА
```

---

## 📊 Поведінка експорту (актуальна)

- Якщо у заявці **один** НП розвантаження: у таблицю потрапляє **один** рядок.
- Якщо у заявці **кілька** НП розвантаження: у таблицю потрапляє **кілька** рядків.
- У Telegram-чат заявка надсилається **одним повідомленням**.
- Для split-рядків таблиці використовуються дочірні ID:
	- головний ID: `A1B2C3D4`
	- рядки в таблиці: `A1B2C3D4-01`, `A1B2C3D4-02`, ...

Примітка: для кількох НП розвантаження поле "Склад розвантаження" автоматично залишається порожнім (`—`).

---

## ✅ Перевірка

Після додавання змінних:
1. Railway автоматично перезапустить бота
2. Перевірте логи: `Successfully exported request to Google Sheets`
3. Створіть тестову заявку:
	- з одним НП розвантаження: має з'явитися 1 рядок,
	- з кількома НП розвантаження: має з'явитися кілька рядків з дочірніми ID.

---

## 🔐 Доступ до таблиці

**ВАЖЛИВО:** Надайте доступ Service Account до вашої Google Таблиці:

1. Відкрийте вашу Google таблицю
2. Натисніть "Share" / "Поділитися"
3. Додайте email вашого Service Account (з JSON файлу, поле `client_email`)
4. Права доступу: **Editor** / **Редактор**
5. Зніміть галочку "Notify people" (щоб не надсилати email)

Без цього кроку бот НЕ зможе писати в таблицю!
