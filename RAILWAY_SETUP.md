# 🚂 Налаштування Railway для Google Sheets

## Змінні середовища на Railway

Додайте ці змінні в Railway Dashboard → Variables:

### 1. Основні змінні (вже є)
```
TELEGRAM_BOT_TOKEN=ваш_токен
TARGET_CHAT_ID=ваш_chat_id
BOT_USERNAME=ваш_username
NOVAPOSHTA_API_KEY=ваш_api_key
DATABASE_URL=postgresql://... (автоматично створюється Railway)
```

### 2. Google Sheets zmінні (ДОДАТИ)

#### GOOGLE_CREDENTIALS_JSON
**Вміст:** Весь JSON з файлу `telegram-bot-requests-486908-d63178aba048.json` одним рядком

**Як додати:**
1. Відкрити файл `telegram-bot-requests-486908-d63178aba048.json`
2. Скопіювати **ВЕСЬ** вміст (від `{` до `}`)
3. Видалити всі переноси рядків (зробити один рядок)
4. Вставити в Railway як значення змінної `GOOGLE_CREDENTIALS_JSON`

**Приклад (скорочено):**
```json
{"type":"service_account","project_id":"telegram-bot-requests-486908","private_key_id":"d63178aba048...","private_key":"-----BEGIN PRIVATE KEY-----\nMIIEvgI...","client_email":"telegram-bot@..."}
```

#### GOOGLE_SPREADSHEET_ID
```
1dg56w5dFYaL9y2aTrwvngo6HjvZ3ct18-3_0nFpJkps
```

#### GOOGLE_WORKSHEET_NAME
```
ЗАЯВКА
```

---

## ✅ Перевірка

Після додавання змінних:
1. Railway автоматично перезапустить бота
2. Перевірте логи: `Successfully exported request to Google Sheets`
3. Створіть тестову заявку - вона має з'явитися в таблиці

---

## 🔐 Доступ до таблиці

**ВАЖЛИВО:** Надайте доступ Service Account до вашої Google Таблиці:

1. Відкрийте таблицю: https://docs.google.com/spreadsheets/d/1dg56w5dFYaL9y2aTrwvngo6HjvZ3ct18-3_0nFpJkps/edit
2. Натисніть "Share" / "Поділитися"
3. Додайте email: `telegram-bot@telegram-bot-requests-486908.iam.gserviceaccount.com`
4. Права доступу: **Editor** / **Редактор**
5. Зніміть галочку "Notify people" (щоб не надсилати email)

Без цього крокау бот НЕ зможе писати в таблицю!
