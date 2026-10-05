#!/usr/bin/env python3
"""
yealink-park-manager
====================

Bulk reboot, autoprovision and SIP NOTIFY management for Yealink IP phones
on FreePBX / Asterisk PJSIP.

Subcommands
-----------
  collect     Собрать список IP Yealink-телефонов в файл
  test        Отправить Reboot на один IP (для проверки)
  reboot      Массовая перезагрузка через Action URI
  autop       Массовый запуск автонастройки через Action URI (AutoP)
  provision   Массовый SIP NOTIFY check-sync

Run `yealink_manager.py <subcommand> -h` for details.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from requests.auth import HTTPBasicAuth
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("[!] Требуется библиотека 'requests'. Установите: pip3 install requests")
    sys.exit(1)


# ------------------------------------------------------------------ #
#  Константы                                                          #
# ------------------------------------------------------------------ #
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"
DEFAULT_SCHEME = "https"
DEFAULT_WORKERS_REBOOT = 20
DEFAULT_WORKERS_PROVISION = 10
DEFAULT_IPS_FILE = "yealink_ips.txt"
DEFAULT_EXTS_FILE = "yealink_extensions.txt"
ASTERISK = "asterisk"


# ------------------------------------------------------------------ #
#  Работа с Asterisk                                                  #
# ------------------------------------------------------------------ #
def run_asterisk(cmd, timeout=60):
    """Выполняет команду в Asterisk CLI и возвращает stdout."""
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
#  Парсинг AstDB                                                      #
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
    Возвращает dict: {'4001': {'ip': '192.168.1.100', 'ua': 'Yealink ...'}, ...}
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


# ------------------------------------------------------------------ #
#  Action URI                                                         #
# ------------------------------------------------------------------ #
class ActionUriClient:
    """Клиент для отправки Action URI на веб-интерфейс телефона."""

    def __init__(self, user, password, scheme, timeout=5):
        self.user = user
        self.password = password
        self.scheme = scheme
        self.timeout = timeout

    def send(self, ip, key):
        schemes = [self.scheme] + (["http"] if self.scheme == "https" else [])
        last_err = None
        for s in schemes:
            url = f"{s}://{ip}/servlet?key={key}"
            try:
                r = requests.get(
                    url,
                    auth=HTTPBasicAuth(self.user, self.password),
                    verify=False,
                    timeout=self.timeout,
                )
                if r.status_code == 200:
                    return True, f"OK {ip} ({s})"
                last_err = f"HTTP {r.status_code} {ip} ({s})"
            except Exception as e:
                last_err = f"ERR {ip} ({s}): {e}"
        return False, last_err

    def reboot(self, ip):
        return self.send(ip, "Reboot")

    def autoprovision(self, ip):
        return self.send(ip, "AutoP")


# ------------------------------------------------------------------ #
#  SIP NOTIFY check-sync                                              #
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
#  Утилиты                                                            #
# ------------------------------------------------------------------ #
def read_file_lines(path):
    if not os.path.exists(path):
        print(f"[!] Нет файла {path}.")
        return []
    with open(path) as f:
        return [l.strip() for l in f if l.strip()]


def write_file_lines(path, lines):
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def run_parallel(items, func, workers, title):
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
#  Subcommands                                                        #
# ------------------------------------------------------------------ #
def cmd_collect(args):
    data = collect_yealink()
    ips = sorted({v["ip"] for v in data.values() if v.get("ip")})
    if not ips:
        print("[!] Yealink-телефоны не найдены.")
        return 1
    write_file_lines(args.ips_file, ips)
    print(f"[+] Сохранено {len(ips)} IP в {args.ips_file}")
    return 0


def cmd_test(args):
    client = ActionUriClient(args.user, args.password, args.scheme, timeout=10)
    print(f"[*] ТЕСТ: Reboot на {args.ip} ({args.scheme}, user={args.user})")
    ok, msg = client.reboot(args.ip)
    print(f"[+] {msg}" if ok else f"[-] {msg}")
    return 0 if ok else 1


def cmd_reboot(args):
    ips = read_file_lines(args.ips_file)
    if not ips:
        return 1
    client = ActionUriClient(args.user, args.password, args.scheme, args.timeout)
    run_parallel(ips, client.reboot, args.workers, "Массовая перезагрузка Yealink")
    return 0


def cmd_autop(args):
    ips = read_file_lines(args.ips_file)
    if not ips:
        return 1
    client = ActionUriClient(args.user, args.password, args.scheme, args.timeout)
    run_parallel(ips, client.autoprovision, args.workers, "Запуск автонастройки (AutoP)")
    return 0


def cmd_provision(args):
    exts = sorted(collect_yealink().keys())
    if not exts:
        print("[!] Yealink-endpoint'ы не найдены.")
        return 1
    write_file_lines(args.exts_file, exts)
    print(f"[+] Сохранено {len(exts)} extension'ов в {args.exts_file}")
    workers = args.workers or DEFAULT_WORKERS_PROVISION
    run_parallel(exts, send_check_sync, workers, "SIP NOTIFY check-sync")
    return 0


# ------------------------------------------------------------------ #
#  CLI                                                                #
# ------------------------------------------------------------------ #
def build_parser():
    p = argparse.ArgumentParser(
        prog="yealink_manager.py",
        description="Bulk management for Yealink IP phones on FreePBX / Asterisk PJSIP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  yealink_manager.py collect\n"
            "  yealink_manager.py test 192.168.1.100 -p secret\n"
            "  yealink_manager.py reboot -w 30 -p secret\n"
            "  yealink_manager.py autop -p secret\n"
            "  yealink_manager.py provision\n"
        ),
    )

    # Общие опции
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-u", "--user",
        default=os.getenv("YEALINK_USER", DEFAULT_USER),
        help=f"Логин веб-интерфейса телефона (по умолчанию: {DEFAULT_USER}, "
             f"env: YEALINK_USER)",
    )
    common.add_argument(
        "-p", "--password",
        default=os.getenv("YEALINK_PASSWORD", DEFAULT_PASSWORD),
        help="Пароль веб-интерфейса (env: YEALINK_PASSWORD)",
    )
    common.add_argument(
        "-s", "--scheme",
        choices=["http", "https"],
        default=os.getenv("YEALINK_SCHEME", DEFAULT_SCHEME),
        help=f"Схема доступа (по умолчанию: {DEFAULT_SCHEME}, env: YEALINK_SCHEME)",
    )
    common.add_argument(
        "-w", "--workers",
        type=int,
        default=None,
        help="Число параллельных потоков (env: MAX_WORKERS)",
    )
    common.add_argument(
        "-t", "--timeout",
        type=int,
        default=5,
        help="Таймаут HTTP-запроса в секундах (по умолчанию: 5)",
    )
    common.add_argument(
        "--ips-file",
        default=DEFAULT_IPS_FILE,
        help=f"Файл со списком IP (по умолчанию: {DEFAULT_IPS_FILE})",
    )
    common.add_argument(
        "--exts-file",
        default=DEFAULT_EXTS_FILE,
        help=f"Файл со списком extension'ов (по умолчанию: {DEFAULT_EXTS_FILE})",
    )

    subs = p.add_subparsers(dest="command", required=True)

    # collect
    p_collect = subs.add_parser(
        "collect",
        parents=[common],
        help="Собрать список IP Yealink-телефонов в файл",
    )
    p_collect.set_defaults(func=cmd_collect)

    # test
    p_test = subs.add_parser(
        "test",
        parents=[common],
        help="Отправить Reboot на один IP (для проверки)",
    )
    p_test.add_argument("ip", help="IP-адрес телефона")
    p_test.set_defaults(func=cmd_test)

    # reboot
    p_reboot = subs.add_parser(
        "reboot",
        parents=[common],
        help="Массовая перезагрузка через Action URI",
    )
    p_reboot.set_defaults(func=cmd_reboot)

    # autop
    p_autop = subs.add_parser(
        "autop",
        parents=[common],
        help="Массовый запуск автонастройки через Action URI (AutoP)",
    )
    p_autop.set_defaults(func=cmd_autop)

    # provision
    p_prov = subs.add_parser(
        "provision",
        parents=[common],
        help="Массовый SIP NOTIFY check-sync",
    )
    p_prov.set_defaults(func=cmd_provision)

    return p


def resolve_workers(args, default):
    """Определяет число потоков: CLI > env > default."""
    if args.workers:
        return args.workers
    env = os.getenv("MAX_WORKERS")
    if env and env.isdigit():
        return int(env)
    return default


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Для reboot/autop дефолт потоков — 20, для provision — 10
    if args.workers is None and args.command in ("reboot", "autop"):
        args.workers = resolve_workers(args, DEFAULT_WORKERS_REBOOT)
    elif args.workers is None and args.command == "provision":
        args.workers = resolve_workers(args, DEFAULT_WORKERS_PROVISION)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
