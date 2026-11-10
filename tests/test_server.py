import http.client
import json
import threading

import pytest

from jobapps import server


@pytest.fixture(scope="module")
def port():
    server.Handler.allowed_hosts = {"127.0.0.1", "localhost", "pc.tailnet.ts.net"}
    server.Handler.allowed_logins = {"owner@github"}
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    r = c.getresponse()
    return r.status, r.read()


def test_local_requests_allowed(port):
    status, body = get(port, "/api/run")
    assert status == 200 and "running" in json.loads(body)
    status, body = get(port, "/")
    assert status == 200 and b"JobApps" in body


def test_funnel_requests_refused(port):
    assert get(port, "/api/run", {"Tailscale-Funnel-Request": "?1"})[0] == 403


def test_dns_rebinding_refused(port):
    assert get(port, "/api/run", {"Host": "evil.example.com"})[0] == 403


def test_tailnet_logins(port, monkeypatch):
    host = {"Host": "pc.tailnet.ts.net:8443"}
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "owner@github"})[0] == 200
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "stranger@github"})[0] == 403
    # until Tailscale says whose PC this is, no login is let in (the PC itself still is)
    monkeypatch.setattr(server.Handler, "allowed_hosts", set(server.Handler.allowed_hosts))
    monkeypatch.setattr(server.Handler, "allowed_logins", set(server.Handler.allowed_logins))
    assert server.set_access({"allowed_hosts": ["pc.tailnet.ts.net"]}, {}) is False
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "stranger@github"})[0] == 403
    assert get(port, "/api/run")[0] == 200
    me = {"dns": "pc.tailnet.ts.net", "ips": ["100.64.0.1"], "login": "Owner@GitHub"}
    assert server.set_access({}, me) is True
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "owner@github"})[0] == 200
    assert server.set_access({"allowed_logins": ["friend@github"]}, me) is True   # the list in config.yaml wins
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "owner@github"})[0] == 403


def test_one_server_per_port(port):
    with pytest.raises(OSError):
        server.Server(("127.0.0.1", port), server.Handler)


def test_static_path_traversal_blocked(port):
    assert get(port, "/../config.yaml")[0] == 404
    assert get(port, "/%2e%2e/config.yaml")[0] == 404


def test_extension_zip_has_server_url(port):
    import io
    import zipfile
    status, body = get(port, "/jobapps-extension.zip", {"Host": "pc.tailnet.ts.net:8443", "Tailscale-User-Login": "owner@github"})
    assert status == 200
    z = zipfile.ZipFile(io.BytesIO(body))
    names = set(z.namelist())
    assert {"jobapps-extension/manifest.json", "jobapps-extension/config.js", "jobapps-extension/filler.js",
            "jobapps-extension/content.js", "jobapps-extension/background.js"} <= names
    assert b"https://pc.tailnet.ts.net:8443" in z.read("jobapps-extension/config.js")
    manifest = json.loads(z.read("jobapps-extension/manifest.json"))
    assert manifest["host_permissions"][0] == "https://pc.tailnet.ts.net:8443/*"


def test_pages_of_one_posting_on_other_sites_match_it():
    from jobapps.sources import posting_keys
    same = [("https://careers.example.com/jobs/91359?icims=1",
             "https://campus-example.icims.com/jobs/91359/graphics-intern/candidate?csrf=AB&in_iframe=1"),
            ("https://example.eightfold.ai/careers/job/446721828828",
             "https://careers.example.com/careers/apply/success?domain=example.com&pid=446721828828"),
            ("https://apply.workable.com/j/9B3CB646E8", "https://apply.workable.com/example/j/9B3CB646E8/apply/")]
    for a, b in same:
        assert posting_keys(a) & posting_keys(b), (a, b)
    assert not posting_keys("https://x.icims.com/jobs/1/a") & posting_keys("https://x.icims.com/jobs/2/a")


def test_a_page_you_found_is_named_after_its_site_not_its_step():
    job = server.adhoc_job("https://careers.example.com/careers/apply/success?pid=4467", "Submit application for Data Intern")
    assert (job["company"], job["title"]) == ("Example", "Data Intern")
    assert server.adhoc_job("https://acme.wd3.myworkdayjobs.com/en-US/search/userHome", "Candidate Home")["company"] == "Acme"
    assert server.site_name("https://campuscanada-acme.icims.com/jobs/1/x/candidate") == "Acme"
    assert server.site_name("https://apply.workable.com/botco/j/9B3CB646E8/apply/") == "Botco"


def post(port, path, headers=None, body=b"{}"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=body, headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    return r.status, r.read()


def test_changes_from_other_web_pages_refused(port):
    # a page anywhere on the web can POST "simple" requests to 127.0.0.1 or the tailnet name; its Origin gives it away
    plain = {"Content-Type": "text/plain"}
    assert post(port, "/api/ext/choose", {"Origin": "https://evil.example.com", **plain})[0] == 403
    assert post(port, "/api/ext/choose", {"Origin": "null", **plain})[0] == 403
    assert post(port, "/api/ext/choose", {"Origin": f"http://127.0.0.1:{port + 1}"})[0] == 403  # another program on this PC
    assert post(port, "/api/ext/choose", {"Origin": f"http://127.0.0.1:{port}"})[0] == 200   # the dashboard
    tailnet = {"Host": "pc.tailnet.ts.net:8443", "Tailscale-User-Login": "owner@github"}
    assert post(port, "/api/ext/choose", {**tailnet, "Origin": "https://pc.tailnet.ts.net:8443"})[0] == 200
    assert post(port, "/api/ext/choose", {**tailnet, "Origin": "https://pc.tailnet.ts.net"})[0] == 403
    assert post(port, "/api/ext/choose", {"Origin": "chrome-extension://abcdefgh"})[0] == 200  # the extension
    assert post(port, "/api/ext/choose")[0] == 200                                             # ja / curl
    assert get(port, "/api/run", {"Origin": "https://evil.example.com"})[0] == 200                 # reads need CORS anyway
    # a form can only post plain text or form fields, so JSON sent as anything else isn't read
    assert post(port, "/api/ext/choose", plain)[0] == 415
    assert post(port, "/api/ext/choose", body=b"[1, 2]")[0] == 400
    assert post(port, "/api/ext/choose", body=b"{not json")[0] == 400


def test_your_answers_are_kept_as_you_left_them(port, tmp_path, monkeypatch):
    from jobapps import db
    from jobapps.forms import Field
    from jobapps.rules import qkey
    path, real = tmp_path / "t.db", db.connect
    monkeypatch.setattr(db, "connect", lambda: real(path))  # not your own database
    conn = real(path)
    why, more = Field("w", "Why this role?", "textarea"), Field("m", "Anything else?", "textarea")
    db.save_form(conn, "a", "greenhouse", "Build internal tools.", [why.to_dict(), more.to_dict()])
    db.save_answer(conn, "a", qkey(why.label), why.label, "The draft.", "", "claude-cli", "m")

    def save(key, text):
        return post(port, "/api/job/a/answer", body=json.dumps({"qkey": key, "text": text}).encode())[0]

    def row(f):
        return db.answers_for(conn, "a").get(qkey(f.label))

    # a question nothing was drafted for still keeps what you wrote
    assert save(qkey(more.label), "Mine.") == 200
    assert (row(more)["question"], row(more)["answer"], db.answer_text(row(more))) == (more.label, None, "Mine.")
    # emptying a box is an edit: the draft doesn't come back, and nothing is filled for it
    assert save(qkey(why.label), "  ") == 200
    assert row(why)["answer"] == "The draft." and db.answer_text(row(why)) == ""
    assert server._question("k", why.label, False, "", row(why))["edited"] is True
    assert db.current_answers(db.answers_for(conn, "a")) == {qkey(more.label): "Mine."}
    # only a redraft puts the draft back
    db.save_final_answer(conn, "a", qkey(why.label), None)
    assert db.answer_text(row(why)) == "The draft." and server._question("k", why.label, False, "", row(why))["edited"] is False
    # a question that isn't on the form, or a body without the text, is refused instead of silently dropped
    assert save("no such question", "x") == 404
    assert post(port, "/api/job/a/answer", body=b'{"qkey": "why this role"}')[0] == 400
    conn.close()


def test_a_resume_that_cant_be_read_doesnt_replace_yours(port, tmp_path, monkeypatch):
    import io

    from pypdf import PdfWriter

    from jobapps import config, drafts
    good, writer = io.BytesIO(), PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(good)
    damaged = b"%PDF-1.7\nnot a PDF past its first line"
    profile, started = config.Profile({"files": {"resume": "resume.pdf"}}, root=tmp_path), []
    monkeypatch.setattr(server, "ROOT", tmp_path)   # not your own folder
    monkeypatch.setattr(server, "load_profile", lambda: profile)
    monkeypatch.setattr(server, "setup_view", lambda profile: {})
    monkeypatch.setattr(server.RUNNER, "start", lambda task, fn: started.append(task) or True)
    monkeypatch.setattr(drafts, "DATA_DIR", tmp_path / "data")

    def put(body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("PUT", "/api/setup/resume", body=body, headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/pdf"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    assert put(good.getvalue())[0] == 200 and started == ["re-pick with your resume"]
    status, body = put(damaged)
    assert status == 400 and "unchanged" in body["error"]
    assert (tmp_path / "resume.pdf").read_bytes() == good.getvalue() and len(started) == 1
    # a damaged file that is already in the folder: no resume text, and the pages still load
    (tmp_path / "resume.pdf").write_bytes(damaged)
    assert drafts.resume_text(profile) == ""
