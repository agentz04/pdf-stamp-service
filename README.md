# PDF Stamp Service (SIG → visual)

Docker-сервис, который берет PDF + внешний файл электронной подписи (.sig / CMS / PKCS#7),
извлекает данные сертификата и времени подписания и добавляет в PDF визуальный штамп.


## Возможности
- Принимает PDF и .sig (CMS/PKCS#7)
- Извлекает из подписи:
  - Сертификат (серийный номер)
  - ФИО подписанта (CN)
  - Дату/время подписания (signingTime)
- Генерирует визуальный штамп и встраивает его в PDF
- Web UI на FastAPI
- Запуск в Docker / Docker Compose

## Стек
Python, FastAPI, OpenSSL, ReportLab, PyPDF, Docker Compose

## Запуск
```bash
docker compose up -d --build

После запуска сервис будет доступен в браузере:

http://<SERVER_IP>:80
