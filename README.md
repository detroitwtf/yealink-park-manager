# yealink-park-manager

> Bulk reboot, autoprovision and SIP NOTIFY management for **Yealink IP phones**
> on **FreePBX / Asterisk PJSIP**.

The tool reads the list of registered Yealink phones straight from Asterisk's
AstDB, filters them by `User-Agent` (so Fanvil, Grandstream and other vendors
are ignored) and lets you centrally:

- **reboot** phones via Action URI (`/servlet?key=Reboot`);
- **force autoprovision** via Action URI (`/servlet?key=AutoP`);
- **send SIP NOTIFY `check-sync`** via `pjsip send notify`.

Designed for parks from a few dozen up to 1000+ devices, and it does not
require any commercial modules such as Endpoint Manager.

**Документация на русском:** [README.ru.md](README.ru.md)

---

## Table of contents

- [Why](#why)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
  - [CLI options](#cli-options)
  - [Environment variables](#environment-variables)
  - [1. Collect the IP list](#1-collect-the-ip-list)
  - [2. Test on a single phone](#2-test-on-a-single-phone)
  - [3. Bulk reboot](#3-bulk-reboot)
  - [4. Bulk autoprovision (AutoP)](#4-bulk-autoprovision-autop)
  - [5. SIP NOTIFY check-sync](#5-sip-notify-check-sync)
- [Typical scenarios](#typical-scenarios)
- [Yealink phone configuration](#yealink-phone-configuration)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Security](#security)
- [Limitations](#limitations)
- [License](#license)

---

## Why

Managing a large fleet of Yealink phones on FreePBX without Endpoint Manager
is painful. Waiting for the natural autoprovision cycle (once a day, sometimes
less often) is not an option when you have 1000+ devices.

This tool solves three problems:

1. **Get an accurate list of IPs** of all Yealink phones registered in
   Asterisk, filtered by vendor.
2. **Broadcast a command** to reboot or re-provision via Action URI.
3. **Send SIP NOTIFY `check-sync`** — a safer alternative to HTTP access
   that does not require phone web credentials.

---

## Features

- 🎯 Auto-collect IPs from AstDB (`database show registrar contact`) with
  a `User-Agent` filter → Yealink only.
- 🔁 Three independent control channels: Action URI (`Reboot`, `AutoP`) and
  SIP NOTIFY (`check-sync`).
- ⚡ Parallel delivery with configurable worker count.
- 🧪 Single-phone test mode before bulk operations.
- 🔐 Credentials via CLI flags or environment variables — nothing hardcoded.
- 🐍 Only stdlib + `requests`.

---

## Requirements

- **FreePBX** 16 or 17 (tested on 17).
- **Asterisk** with PJSIP (chan_sip is not supported).
- **Python** 3.8+.
- **`requests`** (`pip3 install requests`).
- Yealink phones whose firmware supports Action URI and/or SIP Notify
  (see [Yealink phone configuration](#yealink-phone-configuration)).

---

## Installation

```bash
apt update && apt install -y python3-pip
pip3 install requests
git clone https://github.com/<your-login>/yealink-park-manager.git
cd yealink-park-manager
chmod +x yealink_manager.py
```

---

## Usage

### CLI options

```
yealink_manager.py [-h] {collect,test,reboot,autop,provision} ...

Common options (available for every subcommand):
  -u, --user USER         Phone web UI username (default: admin)
  -p, --password PASS     Phone web UI password (default: admin)
  -s, --scheme {http,https}   Access scheme (default: https)
  -w, --workers N         Number of parallel workers
  -t, --timeout SEC       HTTP request timeout in seconds (default: 5)
  --ips-file PATH         File with IP list (default: yealink_ips.txt)
  --exts-file PATH        File with extensions (default: yealink_extensions.txt)
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `YEALINK_USER` | `admin` | Phone web UI username |
| `YEALINK_PASSWORD` | `admin` | Phone web UI password |
| `YEALINK_SCHEME` | `https` | `https` or `http` (with fallback) |
| `MAX_WORKERS` | `20` / `10` | Number of parallel workers |

CLI flags take precedence over environment variables.

### 1. Collect the IP list

```bash
./yealink_manager.py collect
```

Reads AstDB, filters by `user_agent` containing `yealink`, saves unique IPs
to `yealink_ips.txt`.

Expected output:

```
[*] Запрос к базе данных Asterisk...
[*] Строк /registrar/contact/: 984, распарсено JSON: 984, Yealink найдено: 983
[+] Сохранено 983 IP в yealink_ips.txt
```

### 2. Test on a single phone

```bash
./yealink_manager.py test 192.168.1.100 -p 'your-strong-password'
```

Or take the first IP from the already collected file:

```bash
./yealink_manager.py test "$(head -n 1 yealink_ips.txt)" -p 'your-strong-password'
```

Expected output:

```
[*] ТЕСТ: Reboot на 192.168.1.100 (https, user=admin)
[+] OK 192.168.1.100 (https)
```

### 3. Bulk reboot

```bash
./yealink_manager.py reboot -p 'your-strong-password' -w 30
```

With logging:

```bash
./yealink_manager.py reboot -p '...' 2>&1 | tee reboot_$(date +%F_%H%M).log
```

### 4. Bulk autoprovision (AutoP)

Forces each phone to immediately fetch its config from the provisioning
server instead of waiting for the natural cycle:

```bash
./yealink_manager.py autop -p 'your-strong-password'
```

> Requires `features.action_uri.provision = 1` on the phone.

### 5. SIP NOTIFY check-sync

A safer alternative to Action URI — goes over SIP, does not require HTTP
access to the phone, and does not need phone web credentials:

```bash
./yealink_manager.py provision
```

The tool collects Yealink extensions into `yealink_extensions.txt` and sends
`pjsip send notify check-sync endpoint <ext>` to each.

> Requires `features.sip_notify.enable = 1` on the phone.

---

## Typical scenarios

### First-time rollout of a new config

```bash
# 1. Collect current IPs
./yealink_manager.py collect

# 2. Test on one phone
./yealink_manager.py test "$(head -n 1 yealink_ips.txt)" -p 'strong-pass'

# 3. Broadcast AutoP (phones will fetch new config)
./yealink_manager.py autop -p 'strong-pass'

# 4. Wait 10-15 minutes, check provisioning logs
tail -50 /var/log/httpd/access_log

# 5. Once the config has landed — reboot
./yealink_manager.py reboot -p 'strong-pass'
```

### Regular provisioning via cron

```cron
# /etc/cron.d/yealink-provision

# Refresh IP list hourly
0 * * * * root cd /opt/yealink-park-manager && /usr/bin/python3 yealink_manager.py collect > /dev/null 2>&1

# Nightly provisioning
0 3 * * * root cd /opt/yealink-park-manager && /usr/bin/python3 yealink_manager.py provision -p '...' > /var/log/yealink-prov.log 2>&1
```

---

## Yealink phone configuration

For Action URI over HTTP/HTTPS to work, the following parameters must be set
on the phone (via the Yealink config template):

```ini
# Master switch for Action URI
features.action_uri.enable = 1

# Allow reboot via /servlet?key=Reboot
features.action_uri.reboot = 1

# Allow autoprovision via /servlet?key=AutoP
features.action_uri.provision = 1

# Trusted IPs (comma-separated, CIDR allowed)
features.action_uri.allow_ip = 192.168.1.10,192.168.1.0/24

# For SIP NOTIFY (check-sync)
features.sip_notify.enable = 1
```

> Exact parameter names depend on model and firmware version. Verify via
> the phone's web UI (**Settings → Auto Provision** and **Settings → Management**).

---

## Troubleshooting

### `HTTP 403 Forbidden`

The phone received the request but rejected it. Possible causes:

- The IP of the machine sending the request is **not** in
  `features.action_uri.allow_ip`.
- The specific Action URI type is disabled. For example, `Reboot` is enabled
  but `Provision` is not (`features.action_uri.provision = 0`).

**Fix:** check `allow_ip` and make sure all three `features.action_uri.*`
toggles are enabled in the template.

### `HTTP 401 Unauthorized`

Wrong web UI username or password.

**Fix:** verify `-u` / `-p`, ensure the account has administrator privileges.

### `ERR ... timeout` / `ERR ... Connection refused`

The phone is unreachable at this IP. Possible reasons:

- Stale IP (the phone rebooted and got a new DHCP lease).
- Phone is off or lost network.
- Port 80/443 blocked by a firewall.

**Fix:** re-run `./yealink_manager.py collect` to refresh the IP list.

### `Yealink phones not found`

The tool could not find a single Yealink in AstDB. Check:

```bash
asterisk -rx "database show registrar contact" | head -3
```

If the output is empty, no phones are registered (this is not a script issue).

If the output is non-empty but has no `user_agent` or `via_addr`, please
paste an example line into an issue — we'll add support for that format.

### `pjsip send notify check-sync` says "No such notification"

The `check-sync` event is not defined for PJSIP in FreePBX.

**Fix:** add to `/etc/asterisk/pjsip_notify_custom.conf`:

```ini
[check-sync]
Event => check-sync
```

Then run `asterisk -rx "pjsip reload"`.

---

## How it works

1. **Collection.** The tool runs `asterisk -rx "database show registrar contact"`.
   Each AstDB line looks like:
   ```
   /registrar/contact/4001;@hash: {"via_addr":"192.168.1.100", ..., "user_agent":"Yealink SIP-T33G ..."}
   ```
   The extension comes from the key prefix, IP and User-Agent — from the JSON.

2. **Filtering.** Only records whose `user_agent` contains `yealink`
   (case-insensitive) are kept. Fanvil, Grandstream and others are dropped.

3. **Delivery.**
   - **Action URI** — HTTP GET to `http(s)://<IP>/servlet?key=Reboot`
     with Basic Auth.
   - **SIP NOTIFY** — `pjsip send notify check-sync endpoint <ext>`;
     the phone fetches the config from the provisioning server itself.

---

## Security

- **HTTP Basic Auth sends the password in cleartext.** Prefer HTTPS on the
  phones with a valid certificate, or use SIP NOTIFY (no password needed).
- **Never commit passwords.** Use CLI flags or environment variables and
  add a `.env` file to `.gitignore`.
- **Restrict `features.action_uri.allow_ip`.** Do not use `0.0.0.0/0` —
  that exposes phone management to the whole network.

---

## Limitations

- Only **PJSIP** is supported (no `chan_sip`).
- Only **Yealink** is supported (the User-Agent filter is hardcoded).
  PRs for Fanvil / Grandstream / Snom are welcome.
- Only **registered** phones are processed. A powered-off device will not be
  in the list — there is no way to reach it remotely.
- The `database show registrar contact` output may vary across Asterisk
  versions. Tested on Asterisk 20/21 (FreePBX 17).

---

## License

[MIT](LICENSE)
