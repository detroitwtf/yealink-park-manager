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
import getpass
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
def _walk_contact(obj, aor, found):
    """Рекурсивно ищет via_addr/user_agent в JSON-объекте любой вложенности."""
    if isinstance(obj, dict):
        if "via_addr" in obj:
            ua = obj.get("user_agent", "")
            if isinstance(ua, str) and "yealink" in ua.lower():
                # Имя endpoint берём из JSON, AOR из ключа — запасной вариант
                ext = obj.get("endpoint") or aor
                found.setdefault(ext, []).append(
                    {"ip": obj.get("via_addr"), "ua": ua}
                )
            return
        for v in obj.values():
            _walk_contact(v, aor, found)


def collect_yealink():
    """
    Возвращает dict: {'4001': [{'ip': '192.168.1.100', 'ua': 'Yealink ...'}], ...}
    На одном endpoint может быть несколько контактов (max_contacts > 1).
    Endpoint берётся из поля "endpoint" в JSON, иначе из ключа AstDB:
    /registrar/contact/<aor>;@<hash>
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
        aor = key_part.split(";")[0].strip()
        if not aor:
            continue

        try:
            data = json.loads(parts[1].strip())
            parsed += 1
        except json.JSONDecodeError:
            continue

        _walk_contact(data, aor, found)

    contacts = sum(len(v) for v in found.values())
    print(f"[*] Строк /registrar/contact/: {total}, "
          f"распарсено JSON: {parsed}, Yealink найдено: {contacts} "
          f"(endpoint'ов: {len(found)})")
    return found


# ------------------------------------------------------------------ #
#  Action URI                                                         #
# ------------------------------------------------------------------ #
class ActionUriClient:
    """Клиент для отправки Action URI на веб-интерфейс телефона."""

    def __init__(self, user, password, scheme, timeout=5, http_fallback=False):
        self.user = user
        self.password = password
        self.scheme = scheme
        self.timeout = timeout
        self.http_fallback = http_fallback

    def _get(self, scheme, ip, key):
        return requests.get(
            f"{scheme}://{ip}/servlet?key={key}",
            auth=HTTPBasicAuth(self.user, self.password),
            verify=False,
            timeout=self.timeout,
        )

    def send(self, ip, key):
        try:
            r = self._get(self.scheme, ip, key)
        except requests.ConnectionError as e:
            # Откат на HTTP только если HTTPS вообще не отвечает и это явно
            # разрешено: по HTTP пароль Basic Auth уходит открытым текстом.
            if not (self.http_fallback and self.scheme == "https"):
                return False, f"ERR {ip} ({self.scheme}): {e}"
            try:
                r = self._get("http", ip, key)
            except Exception as e2:
                return False, f"ERR {ip} (http): {e2}"
            if r.status_code == 200:
                return True, f"OK {ip} (http)"
            return False, f"HTTP {r.status_code} {ip} (http)"
        except Exception as e:
            return False, f"ERR {ip} ({self.scheme}): {e}"
        if r.status_code == 200:
            return True, f"OK {ip} ({self.scheme})"
        return False, f"HTTP {r.status_code} {ip} ({self.scheme})"

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
        # res_pjsip_notify при успехе печатает
        # "Sending NOTIFY of type 'check-sync' to '<ext>'",
        # при ошибке — строки вида "Unable to ...".
        if out.strip().startswith("Sending NOTIFY") and "Unable to" not in out:
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
        lines = [line.strip() for line in f if line.strip()]
    if not lines:
        print(f"[!] Файл {path} пуст.")
    return lines


def write_file_lines(path, lines):
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def run_parallel(items, func, workers, title):
    """Выполняет func для каждого элемента, возвращает число ошибок."""
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
    return fail


def resolve_password(args):
    """Пароль: CLI > env > интерактивный запрос. Дефолтного пароля нет."""
    if args.password:
        return args.password
    if sys.stdin.isatty():
        return getpass.getpass(f"Пароль веб-интерфейса для {args.user}: ")
    print("[!] Не задан пароль: используйте -p или YEALINK_PASSWORD.",
          file=sys.stderr)
    sys.exit(2)


def make_client(args):
    return ActionUriClient(
        args.user,
        resolve_password(args),
        args.scheme,
        args.timeout,
        http_fallback=args.allow_http_fallback,
    )


# ------------------------------------------------------------------ #
#  Subcommands                                                        #
# ------------------------------------------------------------------ #
def cmd_collect(args):
    data = collect_yealink()
    ips = sorted({c["ip"] for contacts in data.values()
                  for c in contacts if c.get("ip")})
    if not ips:
        print("[!] Yealink-телефоны не найдены.")
        return 1
    write_file_lines(args.ips_file, ips)
    print(f"[+] Сохранено {len(ips)} IP в {args.ips_file}")
    return 0


def cmd_test(args):
    client = make_client(args)
    print(f"[*] ТЕСТ: Reboot на {args.ip} ({args.scheme}, user={args.user})")
    ok, msg = client.reboot(args.ip)
    print(f"[+] {msg}" if ok else f"[-] {msg}")
    return 0 if ok else 1


def cmd_reboot(args):
    ips = read_file_lines(args.ips_file)
    if not ips:
        return 1
    client = make_client(args)
    fail = run_parallel(ips, client.reboot, args.workers,
                        "Массовая перезагрузка Yealink")
    return 1 if fail else 0


def cmd_autop(args):
    ips = read_file_lines(args.ips_file)
    if not ips:
        return 1
    client = make_client(args)
    fail = run_parallel(ips, client.autoprovision, args.workers,
                        "Запуск автонастройки (AutoP)")
    return 1 if fail else 0


def cmd_provision(args):
    exts = sorted(collect_yealink().keys())
    if not exts:
        print("[!] Yealink-endpoint'ы не найдены.")
        return 1
    write_file_lines(args.exts_file, exts)
    print(f"[+] Сохранено {len(exts)} extension'ов в {args.exts_file}")
    workers = args.workers or DEFAULT_WORKERS_PROVISION
    fail = run_parallel(exts, send_check_sync, workers, "SIP NOTIFY check-sync")
    return 1 if fail else 0


# ------------------------------------------------------------------ #
#  CLI                                                                #
# ------------------------------------------------------------------ #
def positive_int(value):
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не число: {value}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"должно быть >= 1: {value}")
    return n


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
        default=os.getenv("YEALINK_PASSWORD"),
        help="Пароль веб-интерфейса (env: YEALINK_PASSWORD; "
             "если не задан — будет запрошен)",
    )
    common.add_argument(
        "-s", "--scheme",
        choices=["http", "https"],
        default=os.getenv("YEALINK_SCHEME", DEFAULT_SCHEME),
        help=f"Схема доступа (по умолчанию: {DEFAULT_SCHEME}, env: YEALINK_SCHEME)",
    )
    common.add_argument(
        "--allow-http-fallback",
        action="store_true",
        help="Если HTTPS недоступен, повторить по HTTP "
             "(пароль уйдёт открытым текстом)",
    )
    common.add_argument(
        "-w", "--workers",
        type=positive_int,
        default=None,
        help="Число параллельных потоков (env: MAX_WORKERS)",
    )
    common.add_argument(
        "-t", "--timeout",
        type=positive_int,
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
    if env and env.isdigit() and int(env) > 0:
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
