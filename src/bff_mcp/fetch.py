"""Public model-link fetcher for Printables and Thingiverse. Stdlib only.

Security model: a link may only be opened when every resolved address of the
host is a global (public) IP, re-checked on every redirect. The stream is
capped at MAX_BYTES. No bytes are stored unless the whole file fits under the
cap.

Endpoints (checked 2026-11-14):
- Printables owns API: POST https://api.printables.com/graphql/ (anonymous OK).
- Thingiverse API: GET https://api.thingiverse.com/things/<id>/files with a
  Bearer app token; anonymous calls are 403 behind the site's bot challenge.
"""
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request

FetchError = type("FetchError", (Exception,), {})

MAX_BYTES = 100 * 1024 * 1024
ALLOWED_HOSTS = {"www.printables.com", "printables.com", "api.printables.com", "files.printables.com",
                 "www.thingiverse.com", "thingiverse.com", "api.thingiverse.com", "cdn.thingiverse.com"}
UA = "BFF/1.0"
PRINTABLE_EXTS = (".stl", ".3mf", ".step", ".stp")
GRAPHQL = "https://api.printables.com/graphql/"
TIMEOUT = 60.0

TOKEN = ""  # Thingiverse app token (setup_thingiverse).


def setup_thingiverse(token):
    global TOKEN
    TOKEN = token or ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_host(newurl, hosts=getattr(req, "bff_redirect_hosts", None))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(_SafeRedirectHandler)


def check_host(url, hosts=None):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise FetchError("This link is not allowed")
    if hosts == "public":
        pass  # any https host whose addresses are all global
    elif parts.hostname not in (hosts if hosts is not None else ALLOWED_HOSTS):
        raise FetchError("This link is not allowed")
    # ponytail: checked at resolve time, the connection resolves again (DNS rebinding window); hosts are allowlisted.
    try:
        addrs = socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise FetchError("Could not download this link") from None
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr[4][0])
        except ValueError:
            raise FetchError("This link is not allowed")
        if not ip.is_global:
            raise FetchError("This link is not allowed")


def open_url(url, data=None, headers=None, redirect_hosts=None):
    check_host(url, hosts=redirect_hosts)
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    auth = hdrs.pop("Authorization", None)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    if auth is not None:
        req.add_unredirected_header("Authorization", auth)
    req.bff_redirect_hosts = redirect_hosts
    return OPENER.open(req, timeout=TIMEOUT)


def read_capped(resp):
    length = resp.headers.get("Content-Length")
    if length is not None and length.isdigit() and int(length) > MAX_BYTES:
        raise FetchError("File is over 100 MB")
    total = 0
    chunks = []
    while True:
        chunk = resp.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise FetchError("File is over 100 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _printable(name):
    return name.lower().endswith(PRINTABLE_EXTS)


def _pick_printable(files):
    """(name, pair) for the first printable file, plus the other printable names."""
    printables = [(f["name"], f) for f in files if _printable(f["name"])]
    if not printables:
        raise FetchError("No printable file on this page")
    primary, pair = printables[0]
    others = [n for n, _ in printables[1:]]
    return primary, pair, others


def _graphql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    with open_url(GRAPHQL, data=body, headers={"Content-Type": "application/json"}) as resp:
        return json.loads(read_capped(resp))


def printables(url):
    m = re.search(r"/model/(\d+)", url)
    if m is None:
        raise FetchError("This link is not allowed")
    model_id = m.group(1)
    data = _graphql(
        "query($id:ID!){model:print(id:$id){id name stls{id name fileSize}}}",
        {"id": model_id})
    name, file_row, others = _pick_printable(data["data"]["model"]["stls"])
    dl = _graphql(
        "mutation($id:ID!,$m:ID!){getDownloadLink(id:$id,printId:$m,fileType:stl,source:model_detail){ok output{link}}}",
        {"id": file_row["id"], "m": model_id})
    link = dl["data"]["getDownloadLink"]["output"]["link"]
    with open_url(link) as resp:
        return name, read_capped(resp), others


def thingiverse(url):
    m = re.search(r"/thing:(\d+)", url)
    if m is None:
        raise FetchError("This link is not allowed")
    if not TOKEN:
        raise FetchError("Thingiverse links need a Thingiverse app token")
    headers = {"Authorization": "Bearer " + TOKEN}
    files_url = f"https://api.thingiverse.com/things/{m.group(1)}/files"
    with open_url(files_url, headers=headers) as resp:
        files = json.loads(read_capped(resp))
    name, entry, others = _pick_printable(files)
    # ponytail: untested against the live API, no token yet.
    with open_url(entry["download_url"], headers=headers) as resp:
        return name, read_capped(resp), others


def _sanitize(name):
    clean = re.sub(r"[^\w.-]", "", name)
    return clean or "model.stl"


def fetch_model(url):
    if not isinstance(url, str):
        raise FetchError("This link is not allowed")
    check_host(url)
    host = urllib.parse.urlsplit(url).hostname
    if host in ("www.printables.com", "printables.com"):
        name, data, others = printables(url)
    elif host in ("www.thingiverse.com", "thingiverse.com"):
        name, data, others = thingiverse(url)
    else:
        raise FetchError("This link is not allowed")
    return _sanitize(name), data, others
