# yealink-park-manager

> Массовая перезагрузка, автонастройка и SIP NOTIFY для **Yealink-телефонов**
> на **FreePBX / Asterisk PJSIP**.

Инструмент читает список зарегистрированных Yealink-телефонов напрямую из
AstDB (базы данных Asterisk), фильтрует их по `User-Agent` (поэтому Fanvil,
Grandstream и другие вендоры игнорируются) и позволяет централизованно:

- **перезагружать** телефоны через Action URI (`/servlet?key=Reboot`);
- **форсировать автонастройку** через Action URI (`/servlet?key=AutoP`);
- **отправлять SIP NOTIFY `check-sync`** через `pjsip send notify`.

Рассчитан на парки от нескольких десятков до 1000+ аппаратов и не требует
установки коммерческих модулей вроде Endpoint Manager.

**Documentation in English:** [README.md](README.md)

---

## Содержание

- [Зачем это нужно](#зачем-это-нужно)
- [Возможности](#возможности)
- [Требования](#требования)
- [Установка](#установка)
- [Использование](#использование)
  - [Опции CLI](#опции-cli)
  - [Переменные окружения](#переменные-окружения)
  - [1. Сбор списка IP](#1-сбор-списка-ip)
  - [2. Тест на одном телефоне](#2-тест-на-одном-телефоне)
  - [3. Массовая перезагрузка](#3-массовая-перезагрузка)
  - [4. Массовая автонастройка (AutoP)](#4-массовая-автонастройка-autop)
  - [5. SIP NOTIFY check-sync](#5-sip-notify-check-sync)
- [Типовые сценарии](#типовые-сценарии)
- [Настройка телефонов Yealink](#настройка-телефонов-yealink)
- [Troubleshooting](#troubleshooting)
- [Как это работает](#как-это-работает)
- [Безопасность](#безопасность)
- [Ограничения](#ограничения)
- [Лицензия](#лицензия)

---

## Зачем это нужно

Массовое управление Yealink-телефонами на FreePBX без Endpoint Manager —
больно. Ждать естественного цикла автонастройки (раз в сутки, а иногда
реже) при 1000 аппаратах — не вариант.

Инструмент решает три задачи:

1. **Получить точный список IP** всех Yealink-телефонов, зарегистрированных
   в Asterisk, с фильтрацией по вендору.
2. **Разослать команду** на перезагрузку или автонастройку через Action URI.
3. **Отправить SIP NOTIFY `check-sync`** — безопасная альтернатива
   HTTP-доступу к телефону, не требующая пароля веб-интерфейса.

---

## Возможности

- 🎯 Автосбор IP из AstDB (`database show registrar contact`) с фильтром
  по `User-Agent` → только Yealink.
- 🔁 Три независимых канала управления: Action URI (`Reboot`, `AutoP`)
  и SIP NOTIFY (`check-sync`).
- ⚡ Параллельная отправка с настраиваемым числом потоков.
- 🧪 Режим теста на одном телефоне перед массовым прогоном.
- 🔐 Учётные данные через флаги CLI или переменные окружения —
  ничего не хардкодится.
- 🐍 Только stdlib + `requests`.

---

## Требования

- **FreePBX** 16 или 17 (тестировалось на 17).
- **Asterisk** с PJSIP (chan_sip не поддерживается).
- **Python** 3.8+.
- **`requests`** (`pip3 install requests`).
- На телефонах Yealink — прошивка, поддерживающая Action URI и/или
  SIP Notify (см. раздел [Настройка телефонов Yealink](#настройка-телефонов-yealink)).

---

## Установка

```bash
apt update && apt install -y python3-pip
pip3 install requests
git clone https://github.com/<ваш-логин>/yealink-park-manager.git
cd yealink-park-manager
chmod +x yealink_manager.py
```

---

## Использование

### Опции CLI

```
yealink_manager.py [-h] {collect,test,reboot,autop,provision} ...

Общие опции (доступны для каждой подкоманды):
  -u, --user USER         Логин веб-интерфейса телефона (по умолчанию: admin)
  -p, --password PASS     Пароль веб-интерфейса (по умолчанию: admin)
  -s, --scheme {http,https}   Схема доступа (по умолчанию: https)
  -w, --workers N         Число параллельных потоков
  -t, --timeout SEC       Таймаут HTTP-запроса в секундах (по умолчанию: 5)
  --ips-file PATH         Файл со списком IP (по умолчанию: yealink_ips.txt)
  --exts-file PATH        Файл со списком extension'ов (по умолчанию: yealink_extensions.txt)
```

### Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `YEALINK_USER` | `admin` | Логин веб-интерфейса телефона |
| `YEALINK_PASSWORD` | `admin` | Пароль веб-интерфейса |
| `YEALINK_SCHEME` | `https` | `https` или `http` (с фолбэком) |
| `MAX_WORKERS` | `20` / `10` | Число параллельных потоков |

Флаги CLI имеют приоритет над переменными окружения.

### 1. Сбор списка IP

```bash
./yealink_manager.py collect
```

Читает AstDB, фильтрует по `user_agent`, содержащему `yealink`,
сохраняет уникальные IP в `yealink_ips.txt`.

Ожидаемый вывод:

```
[*] Запрос к базе данных Asterisk...
[*] Строк /registrar/contact/: 984, распарсено JSON: 984, Yealink найдено: 983
[+] Сохранено 983 IP в yealink_ips.txt
```

### 2. Тест на одном телефоне

```bash
./yealink_manager.py test 192.168.1.100 -p 'ваш-надёжный-пароль'
```

Или взять первый IP из уже собранного файла:

```bash
./yealink_manager.py test "$(head -n 1 yealink_ips.txt)" -p 'ваш-надёжный-пароль'
```

Ожидаемый вывод:

```
[*] ТЕСТ: Reboot на 192.168.1.100 (https, user=admin)
[+] OK 192.168.1.100 (https)
```

### 3. Массовая перезагрузка

```bash
./yealink_manager.py reboot -p 'ваш-надёжный-пароль' -w 30
```

С логированием:

```bash
./yealink_manager.py reboot -p '...' 2>&1 | tee reboot_$(date +%F_%H%M).log
```

### 4. Массовая автонастройка (AutoP)

Заставляет каждый телефон немедленно обратиться к provisioning-серверу
за актуальным конфигом, не дожидаясь естественного цикла:

```bash
./yealink_manager.py autop -p 'ваш-надёжный-пароль'
```

> Требует `features.action_uri.provision = 1` на телефоне.

### 5. SIP NOTIFY check-sync

Безопасная альтернатива Action URI — работает через SIP, не требует
HTTP-доступа к телефону и пароля веб-интерфейса:

```bash
./yealink_manager.py provision
```

Инструмент соберёт extension'ы Yealink-телефонов в `yealink_extensions.txt`
и отправит каждому `pjsip send notify check-sync endpoint <ext>`.

> Требует `features.sip_notify.enable = 1` на телефоне.

---

## Типовые сценарии

### Первичная раскатка нового конфига

```bash
# 1. Собрать актуальный список IP
./yealink_manager.py collect

# 2. Проверить на одном телефоне
./yealink_manager.py test "$(head -n 1 yealink_ips.txt)" -p 'strong-pass'

# 3. Разослать AutoP (телефоны подтянут конфиг)
./yealink_manager.py autop -p 'strong-pass'

# 4. Подождать 10-15 минут, проверить логи провижининга
tail -50 /var/log/httpd/access_log

# 5. Когда конфиг доехал — перезагрузить
./yealink_manager.py reboot -p 'strong-pass'
```

### Регулярный провижининг через cron

```cron
# /etc/cron.d/yealink-provision

# Обновление списка IP каждый час
0 * * * * root cd /opt/yealink-park-manager && /usr/bin/python3 yealink_manager.py collect > /dev/null 2>&1

# Ночной провижининг
0 3 * * * root cd /opt/yealink-park-manager && /usr/bin/python3 yealink_manager.py provision -p '...' > /var/log/yealink-prov.log 2>&1
```

---

## Настройка телефонов Yealink

Для работы Action URI через HTTP/HTTPS на телефоне должны быть
включены соответствующие параметры (в шаблоне конфигурации Yealink):

```ini
# Общий выключатель Action URI
features.action_uri.enable = 1

# Разрешить перезагрузку через /servlet?key=Reboot
features.action_uri.reboot = 1

# Разрешить автонастройку через /servlet?key=AutoP
features.action_uri.provision = 1

# Список доверенных IP (через запятую, можно CIDR)
features.action_uri.allow_ip = 192.168.1.10,192.168.1.0/24

# Для работы SIP NOTIFY (check-sync)
features.sip_notify.enable = 1
```

> Точные имена параметров зависят от модели и версии прошивки.
> Проверяйте через веб-интерфейс телефона
> (**Settings → Auto Provision** и **Settings → Management**).

---

## Troubleshooting

### `HTTP 403 Forbidden`

Телефон получил запрос, но отклонил его. Возможные причины:

- IP-адрес машины, с которой идёт запрос, **не входит** в
  `features.action_uri.allow_ip` на телефоне.
- Запрещён конкретный тип Action URI. Например, разрешён `Reboot`,
  но не разрешён `Provision` (`features.action_uri.provision = 0`).

**Решение:** проверьте `allow_ip` и убедитесь, что все три
`features.action_uri.*` включены в шаблоне.

### `HTTP 401 Unauthorized`

Неверный логин или пароль веб-интерфейса телефона.

**Решение:** проверьте `-u` / `-p`, убедитесь что учётка имеет права
администратора.

### `ERR ... timeout` / `ERR ... Connection refused`

Телефон недоступен по этому IP. Возможные причины:

- IP устарел (телефон перезагрузился, DHCP выдал новый адрес).
- Телефон выключен или потерял сеть.
- Порт 80/443 закрыт файрволом.

**Решение:** заново запустите `./yealink_manager.py collect` для
обновления списка IP.

### `Yealink-телефоны не найдены`

Инструмент не смог найти ни одного Yealink в AstDB. Проверьте:

```bash
asterisk -rx "database show registrar contact" | head -3
```

Если вывод пустой — телефоны не зарегистрированы (проблема не в скрипте).

Если вывод есть, но в строках нет `user_agent` или `via_addr` —
пришлите пример строки в issue, добавим поддержку формата.

### `pjsip send notify check-sync` возвращает "No such notification"

В FreePBX не определён event `check-sync` для PJSIP.

**Решение:** добавьте в `/etc/asterisk/pjsip_notify_custom.conf`:

```ini
[check-sync]
Event => check-sync
```

И выполните `asterisk -rx "pjsip reload"`.

---

## Как это работает

1. **Сбор.** Инструмент выполняет `asterisk -rx "database show registrar contact"`.
   Каждая строка AstDB имеет вид:
   ```
   /registrar/contact/4001;@hash: {"via_addr":"192.168.1.100", ..., "user_agent":"Yealink SIP-T33G ..."}
   ```
   Extension берётся из префикса ключа, IP и User-Agent — из JSON.

2. **Фильтрация.** Оставляем только записи, в `user_agent` которых есть
   подстрока `yealink` (регистронезависимо). Fanvil, Grandstream и другие
   вендоры игнорируются автоматически.

3. **Отправка.**
   - **Action URI** — HTTP GET на `http(s)://<IP>/servlet?key=Reboot`
     с Basic Auth.
   - **SIP NOTIFY** — `pjsip send notify check-sync endpoint <ext>`,
     телефон сам забирает конфиг с provisioning-сервера.

---

## Безопасность

- **HTTP Basic Auth передаёт пароль в открытом виде.** По возможности
  включайте HTTPS на телефонах с валидным сертификатом, либо применяйте
  SIP NOTIFY (там пароль не нужен).
- **Не коммитьте пароли в репозиторий.** Используйте флаги CLI или
  переменные окружения, добавьте `.env` в `.gitignore`.
- **Ограничивайте `features.action_uri.allow_ip`.** Не ставьте
  `0.0.0.0/0` — это открывает управление телефонами всем в сети.

---

## Ограничения

- Поддерживается только **PJSIP** (не `chan_sip`).
- Поддерживаются только **Yealink** (фильтр по User-Agent захардкожен).
  Pull requests для Fanvil / Grandstream / Snom — welcome.
- Работает только с **зарегистрированными** телефонами. Выключенный
  аппарат в список не попадёт — его удалённо не «дёрнуть».
- Формат вывода `database show registrar contact` может отличаться
  между версиями Asterisk. Тестировалось на Asterisk 20/21 (FreePBX 17).

---

## Лицензия

[MIT](LICENSE)
