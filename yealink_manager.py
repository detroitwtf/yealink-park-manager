#!/usr/bin/env python3
"""
yealink-park-manager
====================

Bulk reboot, autoprovision and SIP NOTIFY management for Yealink IP phones
on FreePBX / Asterisk PJSIP.

Subcommands
-----------
  collect     Собрать список IP Yealink-телефонов в файл
  list        Показать таблицу: extension / IP / модель / прошивка
  test        Отправить Reboot на один IP (для проверки)
  reboot      Массовая перезагрузка через Action URI
  autop       Массовый запуск автонастройки через Action URI (AutoP)
  provision   Массовый SIP NOTIFY check-sync

Run `yealink_manager.py <subcommand> -h` for details.
"""
import argparse
import getpass
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from requests.auth import HTTPBasicAuth
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("[!] Требуется библиотека 'requests'. Установите: pip3 install requests")
    sys.exit(1)


__version__ = "0.2.0"

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
def parse_ua(ua):
    """'Yealink SIP-T33G 124.86.0.75' -> ('T33G', '124.86.0.75')."""
    tokens = ua.split()
    if tokens and tokens[0].lower() == "yealink":
        tokens = tokens[1:]
    firmware = ""
    if tokens and re.fullmatch(r"\d+(\.\d+)+", tokens[-1]):
        firmware = tokens.pop()
    model = " ".join(tokens)
    for prefix in ("SIP-", "SIP "):
        if model.startswith(prefix):
            model = model[len(prefix):]
    return model or "?", firmware or "?"


def _walk_contact(obj, aor, found):
    """Рекурсивно ищет via_addr/user_agent в JSON-объекте любой вложенности."""
    if isinstance(obj, dict):
        if "via_addr" in obj:
            ua = obj.get("user_agent", "")
            if isinstance(ua, str) and "yealink" in ua.lower():
                model, firmware = parse_ua(ua)
                found.append({
                    # Имя endpoint берём из JSON, AOR из ключа — запасной вариант
                    "ext": obj.get("endpoint") or aor,
                    "ip": obj.get("via_addr"),
                    "ua": ua,
                    "model": model,
                    "firmware": firmware,
                })
            return
        for v in obj.values():
            _walk_contact(v, aor, found)


def parse_contacts(out):
    """
    Разбирает вывод `database show registrar contact`.
    Возвращает (contacts, total, parsed), где contacts — список dict:
    {'ext', 'ip', 'ua', 'model', 'firmware'}. На одном endpoint может быть
    несколько контактов (max_contacts > 1).
    Endpoint берётся из поля "endpoint" в JSON, иначе из ключа AstDB:
    /registrar/contact/<aor>;@<hash>
    """
    found = []
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

    return found, total, parsed


def collect_yealink():
    """Читает AstDB и возвращает список контактов Yealink."""
    print("[*] Запрос к базе данных Asterisk...", file=sys.stderr)
    contacts, total, parsed = parse_contacts(
        run_asterisk("database show registrar contact")
    )
    exts = {c["ext"] for c in contacts}
    print(f"[*] Строк /registrar/contact/: {total}, "
          f"распарсено JSON: {parsed}, Yealink найдено: {len(contacts)} "
          f"(endpoint'ов: {len(exts)})", file=sys.stderr)
    return contacts


# ------------------------------------------------------------------ #
#  Фильтры                                                            #
# ------------------------------------------------------------------ #
def parse_ext_filter(spec):
    """'4001,4010-4020' -> функция-предикат для extension."""
    exact, ranges = set(), []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            if not (lo.isdigit() and hi.isdigit()):
                raise argparse.ArgumentTypeError(f"неверный диапазон: {part}")
            ranges.append((int(lo), int(hi)))
        else:
            exact.add(part)

    def match(ext):
        if ext in exact:
            return True
        return ext.isdigit() and any(lo <= int(ext) <= hi for lo, hi in ranges)

    return match


def parse_subnets(spec):
    """'10.1.0.0/16,10.2.0.0/16' -> список ip_network."""
    try:
        return [ipaddress.ip_network(s.strip(), strict=False)
                for s in spec.split(",") if s.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def ip_in(ip, subnets):
    try:
        addr = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return False
    return any(addr in net for net in subnets)


def has_filters(args):
    return bool(args.ext or args.model or args.subnet)


def filter_contacts(contacts, args):
    result = []
    for c in contacts:
        if args.ext and not args.ext(c["ext"]):
            continue
        if args.model and args.model.lower() not in c["model"].lower():
            continue
        if args.subnet and not ip_in(c["ip"], args.subnet):
            continue
        result.append(c)
    return result


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
    """Выполняет func для каждого элемента, возвращает список неудачных."""
    print(f"\n[*] {title} ({len(items)} шт.)...")
    ok = 0
    failed, messages = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(func, item): item for item in items}
        for fut in as_completed(futures):
            success, msg = fut.result()
            if success:
                ok += 1
                print(f"[+] {msg}")
            else:
                failed.append(futures[fut])
                messages.append(msg)
                print(f"[-] {msg}")
    print(f"\n[*] Готово. Успешно: {ok}, Ошибок: {len(failed)}")
    if messages:
        print("[*] Неудачные:")
        for m in messages:
            print(f"    {m}")
    return failed


def run_bulk(items, func, args, title):
    """Запуск волнами (--batch/--delay) с сохранением неудачных в файл."""
    batch = args.batch or len(items)
    waves = (len(items) + batch - 1) // batch
    failed = []
    for n, start in enumerate(range(0, len(items), batch), 1):
        if n > 1 and args.delay:
            print(f"\n[*] Пауза {args.delay:g} с перед волной {n}/{waves}...")
            time.sleep(args.delay)
        label = title if waves == 1 else f"{title}, волна {n}/{waves}"
        failed += run_parallel(items[start:start + batch], func,
                               args.workers, label)
    if waves > 1:
        print(f"\n[*] Итого: успешно {len(items) - len(failed)}, "
              f"ошибок {len(failed)}")
    if failed and args.failed_file:
        write_file_lines(args.failed_file, failed)
        print(f"[*] Неудачные ({len(failed)}) сохранены в {args.failed_file}")
    return 1 if failed else 0


def dry_run(items, action):
    print(f"[*] DRY RUN: {action} — {len(items)} шт. (ничего не отправляется)")
    for item in items:
        print(f"    {item}")
    return 0


def confirm(args, question):
    """Спрашивает подтверждение; без TTY требует --yes."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("[!] Нет терминала для подтверждения: добавьте --yes.",
              file=sys.stderr)
        return False
    answer = input(f"{question} [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


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


def action_targets(args):
    """
    IP для reboot/autop. С --ext/--model список берётся из AstDB заново
    (в файле IP нет extension и модели), иначе — из --ips-file.
    """
    if args.ext or args.model:
        contacts = filter_contacts(collect_yealink(), args)
        return sorted({c["ip"] for c in contacts if c.get("ip")})
    ips = read_file_lines(args.ips_file)
    if args.subnet:
        ips = [ip for ip in ips if ip_in(ip, args.subnet)]
    return ips


# ------------------------------------------------------------------ #
#  Subcommands                                                        #
# ------------------------------------------------------------------ #
def cmd_collect(args):
    contacts = filter_contacts(collect_yealink(), args)
    ips = sorted({c["ip"] for c in contacts if c.get("ip")})
    if not ips:
        print("[!] Yealink-телефоны не найдены.")
        return 1
    write_file_lines(args.ips_file, ips)
    print(f"[+] Сохранено {len(ips)} IP в {args.ips_file}")
    return 0


def cmd_list(args):
    contacts = sorted(filter_contacts(collect_yealink(), args),
                      key=lambda c: (c["ext"], c["ip"] or ""))
    if args.json:
        print(json.dumps(contacts, ensure_ascii=False, indent=2))
        return 0 if contacts else 1
    if not contacts:
        print("[!] Yealink-телефоны не найдены.")
        return 1
    header = ("EXT", "IP", "MODEL", "FIRMWARE")
    rows = [(c["ext"], c["ip"] or "?", c["model"], c["firmware"])
            for c in contacts]
    widths = [max(len(r[i]) for r in rows + [header]) for i in range(4)]
    for row in [header] + rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())
    return 0


def cmd_test(args):
    client = make_client(args)
    print(f"[*] ТЕСТ: Reboot на {args.ip} ({args.scheme}, user={args.user})")
    ok, msg = client.reboot(args.ip)
    print(f"[+] {msg}" if ok else f"[-] {msg}")
    return 0 if ok else 1


def _action(args, method, title, question):
    ips = action_targets(args)
    if not ips:
        if has_filters(args):
            print("[!] Под фильтры не попал ни один телефон.")
        return 1
    if args.dry_run:
        return dry_run(ips, title)
    if not confirm(args, question.format(n=len(ips))):
        print("[*] Отменено.")
        return 1
    client = make_client(args)
    return run_bulk(ips, getattr(client, method), args, title)


def cmd_reboot(args):
    return _action(args, "reboot", "Массовая перезагрузка Yealink",
                   "Перезагрузить {n} телефонов?")


def cmd_autop(args):
    return _action(args, "autoprovision", "Запуск автонастройки (AutoP)",
                   "Запустить AutoP на {n} телефонах?")


def cmd_provision(args):
    contacts = filter_contacts(collect_yealink(), args)
    exts = sorted({c["ext"] for c in contacts})
    if not exts:
        print("[!] Yealink-endpoint'ы не найдены.")
        return 1
    if args.dry_run:
        return dry_run(exts, "SIP NOTIFY check-sync")
    if not confirm(args, f"Отправить check-sync на {len(exts)} endpoint'ов?"):
        print("[*] Отменено.")
        return 1
    write_file_lines(args.exts_file, exts)
    print(f"[+] Сохранено {len(exts)} extension'ов в {args.exts_file}")
    return run_bulk(exts, send_check_sync, args, "SIP NOTIFY check-sync")


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


def non_negative_float(value):
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не число: {value}")
    if n < 0:
        raise argparse.ArgumentTypeError(f"должно быть >= 0: {value}")
    return n


def build_parser():
    p = argparse.ArgumentParser(
        prog="yealink_manager.py",
        description="Bulk management for Yealink IP phones on FreePBX / Asterisk PJSIP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  yealink_manager.py collect\n"
            "  yealink_manager.py list --model T33G\n"
            "  yealink_manager.py test 192.168.1.100 -p secret\n"
            "  yealink_manager.py reboot --dry-run\n"
            "  yealink_manager.py reboot -p secret --batch 50 --delay 30\n"
            "  yealink_manager.py autop -p secret --ext 4001-4099\n"
            "  yealink_manager.py provision --yes\n"
        ),
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")

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

    # Фильтры
    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument(
        "--ext",
        type=parse_ext_filter,
        help="Только эти extension'ы: 4001,4005,4010-4020",
    )
    filters.add_argument(
        "--subnet",
        type=parse_subnets,
        help="Только эти подсети: 10.1.0.0/16,10.2.0.0/24",
    )
    filters.add_argument(
        "--model",
        help="Только модели, содержащие подстроку (без учёта регистра): T33G",
    )

    # Опции массовых операций
    bulk = argparse.ArgumentParser(add_help=False)
    bulk.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Показать список целей и ничего не отправлять",
    )
    bulk.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Не спрашивать подтверждение (обязательно для cron)",
    )
    bulk.add_argument(
        "--batch",
        type=positive_int,
        help="Отправлять волнами по N устройств",
    )
    bulk.add_argument(
        "--delay",
        type=non_negative_float,
        default=0,
        help="Пауза между волнами в секундах (по умолчанию: 0)",
    )
    bulk.add_argument(
        "--failed-file",
        help="Сохранить неудачные IP/extension'ы в файл для повторного прогона",
    )

    subs = p.add_subparsers(dest="command", required=True)

    # collect
    p_collect = subs.add_parser(
        "collect",
        parents=[common, filters],
        help="Собрать список IP Yealink-телефонов в файл",
    )
    p_collect.set_defaults(func=cmd_collect)

    # list
    p_list = subs.add_parser(
        "list",
        parents=[common, filters],
        help="Показать таблицу: extension / IP / модель / прошивка",
    )
    p_list.add_argument("--json", action="store_true", help="Вывод в JSON")
    p_list.set_defaults(func=cmd_list)

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
        parents=[common, filters, bulk],
        help="Массовая перезагрузка через Action URI",
    )
    p_reboot.set_defaults(func=cmd_reboot)

    # autop
    p_autop = subs.add_parser(
        "autop",
        parents=[common, filters, bulk],
        help="Массовый запуск автонастройки через Action URI (AutoP)",
    )
    p_autop.set_defaults(func=cmd_autop)

    # provision
    p_prov = subs.add_parser(
        "provision",
        parents=[common, filters, bulk],
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # Для reboot/autop дефолт потоков — 20, для provision — 10
    if args.workers is None and args.command in ("reboot", "autop"):
        args.workers = resolve_workers(args, DEFAULT_WORKERS_REBOOT)
    elif args.workers is None and args.command == "provision":
        args.workers = resolve_workers(args, DEFAULT_WORKERS_PROVISION)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
