import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from jobapps import server


@pytest.fixture(scope="module")
def port():
    server.Handler.allowed_hosts = {"127.0.0.1", "localhost", "pc.tailnet.ts.net"}
    server.Handler.allowed_logins = {"owner@github"}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
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


def test_tailnet_logins(port):
    host = {"Host": "pc.tailnet.ts.net:8443"}
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "owner@github"})[0] == 200
    assert get(port, "/api/run", {**host, "Tailscale-User-Login": "stranger@github"})[0] == 403


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
