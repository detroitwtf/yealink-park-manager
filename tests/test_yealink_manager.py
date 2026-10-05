import argparse

import pytest
import requests

import yealink_manager as ym

ASTDB_OUTPUT = """\
/registrar/contact/4001;@aaa                     : {"via_addr":"10.0.0.1","user_agent":"Yealink SIP-T33G 124.86.0.75","endpoint":"4001"}
/registrar/contact/4001;@bbb                     : {"via_addr":"10.0.0.2","user_agent":"Yealink SIP-T31P 124.86.0.40","endpoint":"4001"}
/registrar/contact/4002;@ccc                     : {"via_addr":"10.0.0.3","user_agent":"Fanvil X3S 2.4.0"}
/registrar/contact/4003;@ddd                     : {"via_addr":"10.1.0.4","user_agent":"Yealink W60B 77.85.0.25"}
/registrar/contact/broken;@eee                   : {not json}
4 results found.
"""


@pytest.fixture
def astdb(monkeypatch):
    """Подменяет Asterisk: AstDB из ASTDB_OUTPUT, NOTIFY успешен кроме 4003."""
    calls = []

    def fake(cmd, timeout=60):
        calls.append(cmd)
        if cmd == "database show registrar contact":
            return ASTDB_OUTPUT
        if cmd.endswith("endpoint 4003"):
            return "Unable to retrieve endpoint 4003\n"
        ext = cmd.rsplit(" ", 1)[1]
        return f"Sending NOTIFY of type 'check-sync' to '{ext}'\n"

    monkeypatch.setattr(ym, "run_asterisk", fake)
    return calls


@pytest.fixture
def no_http(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("HTTP-запрос не должен был уйти")

    monkeypatch.setattr(ym.requests, "get", fail)


def run(argv):
    with pytest.raises(SystemExit) as e:
        ym.main(argv)
    return e.value.code


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize("ua, expected", [
    ("Yealink SIP-T33G 124.86.0.75", ("T33G", "124.86.0.75")),
    ("Yealink SIP VP-T49G 51.80.0.130", ("VP-T49G", "51.80.0.130")),
    ("Yealink W60B 77.85.0.25", ("W60B", "77.85.0.25")),
    ("Yealink SIP-T46S", ("T46S", "?")),
    ("Yealink", ("?", "?")),
])
def test_parse_ua(ua, expected):
    assert ym.parse_ua(ua) == expected


def test_parse_contacts_keeps_all_contacts_and_skips_other_vendors():
    contacts, total, parsed = ym.parse_contacts(ASTDB_OUTPUT)
    assert (total, parsed) == (5, 4)
    assert [(c["ext"], c["ip"], c["model"]) for c in contacts] == [
        ("4001", "10.0.0.1", "T33G"),
        ("4001", "10.0.0.2", "T31P"),
        ("4003", "10.1.0.4", "W60B"),
    ]


def test_parse_contacts_prefers_endpoint_field_over_aor():
    line = ('/registrar/contact/aor1;@x : '
            '{"via_addr":"10.0.0.9","user_agent":"Yealink T54W","endpoint":"ep1"}')
    contacts, _, _ = ym.parse_contacts(line)
    assert contacts[0]["ext"] == "ep1"


# ---------------------------------------------------------------- filters


def test_ext_filter():
    match = ym.parse_ext_filter("4001,4010-4020")
    assert match("4001") and match("4010") and match("4020")
    assert not match("4002") and not match("4021") and not match("abc")


def test_ext_filter_rejects_bad_range():
    with pytest.raises(argparse.ArgumentTypeError):
        ym.parse_ext_filter("40a-40b")


def test_ip_in():
    nets = ym.parse_subnets("10.1.0.0/16")
    assert ym.ip_in("10.1.2.3", nets)
    assert not ym.ip_in("10.0.0.1", nets)
    assert not ym.ip_in("phone.local", nets)
    assert not ym.ip_in(None, nets)


# ---------------------------------------------------------------- NOTIFY


def test_send_check_sync(astdb):
    assert ym.send_check_sync("4001") == (True, "OK 4001")
    ok, msg = ym.send_check_sync("4003")
    assert not ok and "Unable to retrieve endpoint" in msg


# ---------------------------------------------------------------- Action URI


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_no_http_fallback_by_default(monkeypatch):
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(ym.requests, "get", fake_get)
    ok, _ = ym.ActionUriClient("admin", "x", "https").reboot("10.0.0.1")
    assert not ok
    assert urls == ["https://10.0.0.1/servlet?key=Reboot"]


def test_http_fallback_only_on_connection_error(monkeypatch):
    def fake_get(url, **kw):
        if url.startswith("https"):
            raise requests.ConnectionError("refused")
        return FakeResponse(200)

    monkeypatch.setattr(ym.requests, "get", fake_get)
    client = ym.ActionUriClient("admin", "x", "https", http_fallback=True)
    assert client.reboot("10.0.0.1") == (True, "OK 10.0.0.1 (http)")


def test_no_fallback_on_401(monkeypatch):
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        return FakeResponse(401)

    monkeypatch.setattr(ym.requests, "get", fake_get)
    client = ym.ActionUriClient("admin", "x", "https", http_fallback=True)
    ok, msg = client.reboot("10.0.0.1")
    assert not ok and "HTTP 401" in msg
    assert len(urls) == 1


# ---------------------------------------------------------------- CLI


def test_list_json_with_model_filter(astdb, capsys):
    assert run(["list", "--json", "--model", "t33"]) == 0
    out = capsys.readouterr().out
    assert '"ip": "10.0.0.1"' in out and "10.0.0.2" not in out


def test_collect_with_subnet_filter(astdb, tmp_path):
    ips_file = tmp_path / "ips.txt"
    assert run(["collect", "--subnet", "10.0.0.0/24",
                "--ips-file", str(ips_file)]) == 0
    assert ips_file.read_text().split() == ["10.0.0.1", "10.0.0.2"]


def test_reboot_dry_run_sends_nothing(tmp_path, no_http, capsys):
    ips_file = tmp_path / "ips.txt"
    ips_file.write_text("10.0.0.1\n10.0.0.2\n")
    assert run(["reboot", "--dry-run", "--ips-file", str(ips_file)]) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_reboot_without_tty_requires_yes(tmp_path, no_http, monkeypatch):
    monkeypatch.setattr(ym.sys.stdin, "isatty", lambda: False)
    ips_file = tmp_path / "ips.txt"
    ips_file.write_text("10.0.0.1\n")
    assert run(["reboot", "-p", "x", "--ips-file", str(ips_file)]) == 1


def test_reboot_batches_and_failed_file(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ym.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        ym.requests, "get",
        lambda url, **kw: FakeResponse(403 if "10.0.0.3" in url else 200),
    )
    ips_file = tmp_path / "ips.txt"
    ips_file.write_text("".join(f"10.0.0.{i}\n" for i in range(1, 6)))
    failed_file = tmp_path / "failed.txt"
    code = run(["reboot", "-p", "x", "--yes", "--ips-file", str(ips_file),
                "--batch", "2", "--delay", "1.5",
                "--failed-file", str(failed_file)])
    assert code == 1
    assert sleeps == [1.5, 1.5]  # 3 волны → 2 паузы
    assert failed_file.read_text().split() == ["10.0.0.3"]


def test_provision_with_ext_filter(astdb, tmp_path):
    code = run(["provision", "--yes", "--ext", "4001",
                "--exts-file", str(tmp_path / "exts.txt")])
    assert code == 0
    assert "pjsip send notify check-sync endpoint 4001" in astdb
    assert not any(c.endswith("4003") for c in astdb)


def test_provision_reports_failures(astdb, tmp_path):
    code = run(["provision", "--yes",
                "--exts-file", str(tmp_path / "exts.txt")])
    assert code == 1


def test_version(capsys):
    assert run(["--version"]) == 0
    assert ym.__version__ in capsys.readouterr().out
