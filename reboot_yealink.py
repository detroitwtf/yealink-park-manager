#!/usr/bin/env python3
"""
Управление парком Yealink-телефонов на FreePBX 17 / Asterisk PJSIP.

Режимы:
  (без флагов)      — собрать список IP Yealink-телефонов в yealink_ips.txt
  --test <IP>       — отправить Reboot на один IP (для проверки)
  --reboot          — массовая перезагрузка через Action URI
  --autop           — массовый запуск автонастройки через Action URI (AutoP)
  --provision       — массовый SIP NOTIFY check-sync

Переменные окружения:
  YEALINK_USER      логин веб-интерфейса (по умолчанию: admin)
  YEALINK_PASSWORD  пароль веб-интерфейса (по умолчанию: admin)
  YEALINK_SCHEME    https или http (по умолчанию: https, с фолбэком на http)
  MAX_WORKERS       число параллельных потоков (по умолчанию: 20)
"""
import subprocess
import json
import sys
import os
import requests
from requests.auth import HTTPBasicAuth
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ASTERISK = "asterisk"
IPS_FILE = "yealink_ips.txt"
EXTS_FILE = "yealink_extensions.txt"


# ------------------------------------------------------------------ #
#  Работа с Asterisk                                                  #
# ------------------------------------------------------------------ #
def run_asterisk(cmd, timeout=60):
    try:
        return subprocess.check_output(
            [ASTERISK, "-rx", cmd],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        print(f"[!] Ошибка 'asterisk -rx {cmd}': {e.output}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[!] Ошибка запуска Asterisk: {e}", file=sys.stderr)
        return ""


# ------------------------------------------------------------------ #
#  Парсинг AstDB (registrar contact)                                  #
# ------------------------------------------------------------------ #
def _walk_contact(obj, ext, found):
    """Рекурсивно ищет via_addr/user_agent в JSON-объекте любой вложенности."""
    if isinstance(obj, dict):
        if "via_addr" in obj:
            ua = obj.get("user_agent", "")
            if isinstance(ua, str) and "yealink" in ua.lower():
                found[ext] = {"ip": obj.get("via_addr"), "ua": ua}
            return
        for v in obj.values():
            _walk_contact(v, ext, found)


def collect_yealink():
    """
    Возвращает dict: {'4001': {'ip': '10.0.0.1', 'ua': 'Yealink ...'}, ...}
    Extension берётся из ключа AstDB: /registrar/contact/<ext>;@<hash>
    """
    print("[*] Запрос к базе данных Asterisk...")
    out = run_asterisk("database show registrar contact")
    found = {}
    total = parsed = 0

    for line in out.splitlines():
        if not line.startswith("/registrar/contact/"):
            continue
        total += 1

        parts = line.split(": ", 1)
        if len(parts) != 2:
            continue

        # extension из ключа AstDB
        key_part = parts[0].replace("/registrar/contact/", "", 1)
        ext = key_part.split(";")[0].strip()
        if not ext:
            continue

        try:
            data = json.loads(parts[1].strip())
            parsed += 1
        except json.JSONDecodeError:
            continue

        _walk_contact(data, ext, found)

    print(f"[*] Строк /registrar/contact/: {total}, "
          f"распарсено JSON: {parsed}, Yealink найдено: {len(found)}")
    return found


def collect_yealink_ips():
    data = collect_yealink()
    return sorted({v["ip"] for v in data.values() if v.get("ip")})


def collect_yealink_extensions():
    return sorted(collect_yealink().keys())


# ------------------------------------------------------------------ #
#  Action URI (HTTP-запросы к веб-интерфейсу телефона)                #
# ------------------------------------------------------------------ #
def _action_uri(ip, key, timeout=5):
    user = os.getenv("YEALINK_USER", "admin")
    password = os.getenv("YEALINK_PASSWORD", "admin")
    scheme = os.getenv("YEALINK_SCHEME", "https")
    schemes = [scheme] + (["http"] if scheme == "https" else [])

    last_err = None
    for s in schemes:
        url = f"{s}://{ip}/servlet?key={key}"
        try:
            r = requests.get(
                url,
                auth=HTTPBasicAuth(user, password),
                verify=False,
                timeout=timeout,
            )
            if r.status_code == 200:
                return True, f"OK {ip} ({s})"
            last_err = f"HTTP {r.status_code} {ip} ({s})"
        except Exception as e:
            last_err = f"ERR {ip} ({s}): {e}"
    return False, last_err


def reboot_phone(ip, timeout=5):
    return _action_uri(ip, "Reboot", timeout)


def autoprovision_phone(ip, timeout=5):
    return _action_uri(ip, "AutoP", timeout)


# ------------------------------------------------------------------ #
#  SIP NOTIFY check-sync                                             #
# ------------------------------------------------------------------ #
def send_check_sync(ext):
    try:
        out = run_asterisk(f"pjsip send notify check-sync endpoint {ext}", timeout=10)
        if "successfully" in out.lower() or "accepted" in out.lower():
            return True, f"OK {ext}"
        return False, f"{ext}: {out.strip() or 'no output'}"
    except Exception as e:
        return False, f"{ext}: {e}"


# ------------------------------------------------------------------ #
#  Вспомогательные                                                   #
# ------------------------------------------------------------------ #
def _read_ips():
    if not os.path.exists(IPS_FILE):
        print(f"[!] Нет файла {IPS_FILE}. Сначала запустите сбор без флагов.")
        return []
    with open(IPS_FILE) as f:
        return [l.strip() for l in f if l.strip()]


def _run_parallel(items, func, workers, title):
    print(f"\n[*] {title} ({len(items)} шт.)...")
    ok = fail = 0
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(func, item): item for item in items}
        for fut in as_completed(futures):
            success, msg = fut.result()
            if success:
                ok += 1
                print(f"[+] {msg}")
            else:
                fail += 1
                failed.append(msg)
                print(f"[-] {msg}")
    print(f"\n[*] Готово. Успешно: {ok}, Ошибок: {fail}")
    if failed:
        print("[*] Неудачные:")
        for m in failed:
            print(f"    {m}")


# ------------------------------------------------------------------ #
#  Режимы                                                            #
# ------------------------------------------------------------------ #
def mode_collect():
    ips = collect_yealink_ips()
    if not ips:
        print("[!] Yealink-телефоны не найдены.")
        return
    with open(IPS_FILE, "w") as f:
        f.write("\n".join(ips) + "\n")
    print(f"[+] Сохранено {len(ips)} IP в {IPS_FILE}")


def mode_test(ip):
    user = os.getenv("YEALINK_USER", "admin")
    scheme = os.getenv("YEALINK_SCHEME", "https")
    print(f"[*] ТЕСТ: Reboot на {ip} ({scheme}, user={user})")
    ok, msg = reboot_phone(ip, timeout=10)
    print(f"[+] {msg}" if ok else f"[-] {msg}")


def mode_reboot():
    ips = _read_ips()
    if not ips:
        return
    workers = int(os.getenv("MAX_WORKERS", "20"))
    _run_parallel(ips, reboot_phone, workers, "Массовая перезагрузка Yealink")


def mode_autop():
    ips = _read_ips()
    if not ips:
        return
    workers = int(os.getenv("MAX_WORKERS", "20"))
    _run_parallel(ips, autoprovision_phone, workers, "Запуск автонастройки (AutoP)")


def mode_provision():
    exts = collect_yealink_extensions()
    if not exts:
        print("[!] Yealink-endpoint'ы не найдены.")
        return
    with open(EXTS_FILE, "w") as f:
        f.write("\n".join(exts) + "\n")
    print(f"[+] Сохранено {len(exts)} extension'ов в {EXTS_FILE}")
    workers = int(os.getenv("MAX_WORKERS", "10"))
    _run_parallel(exts, send_check_sync, workers, "SIP NOTIFY check-sync")


# ------------------------------------------------------------------ #
#  Точка входа                                                       #
# ------------------------------------------------------------------ #
def main():
    args = sys.argv[1:]

    if "--test" in args:
        idx = args.index("--test")
        if idx + 1 >= len(args):
            print("Использование: --test <IP>")
            return
        mode_test(args[idx + 1])
        return

    if "--reboot" in args:
        mode_reboot()
        return

    if "--autop" in args:
        mode_autop()
        return

    if "--provision" in args:
        mode_provision()
        return

    mode_collect()


if __name__ == "__main__":
    main()
