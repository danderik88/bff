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
import io
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
import zipfile

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


BUNDLE_EXT = ".bundle.zip"  # several meshes from one page, sliced together (2026-11-17)


def bundle(files):
    """Zip of [(name, bytes)] meshes; one file stays as it is."""
    if len(files) == 1:
        return files[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, data) in enumerate(files):
            z.writestr(f"{i:03d}_{_sanitize(name)}", data)
    return files[0][0].rsplit(".", 1)[0] + BUNDLE_EXT, buf.getvalue()


def bundle_names(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [n[4:] for n in z.namelist()]


def bundle_pick(data, keep):
    """The bundle with only the files at indexes `keep` (a single file comes back bare)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        chosen = [(names[i][4:], z.read(names[i])) for i in sorted(set(keep)) if 0 <= i < len(names)]
    if not chosen:
        raise FetchError("Pick at least one file")
    return bundle(chosen)


def _download_all(rows, link_of):
    """Every mesh on the page, 100 MB in all. A 3MF next to meshes is usually the same model as a project,
    so it stays aside; a page of 3MFs only (parts or variants) offers them all."""
    printable = [f for f in rows if _printable(f["name"])]
    meshes = [f for f in printable if not f["name"].lower().endswith(".3mf")] or printable
    if len(meshes) < 2:
        name, row, others = _pick_printable(rows)
        return name, link_of(row), others
    files, total = [], 0
    for row in meshes:
        data = link_of(row)
        total += len(data)
        if total > MAX_BYTES:
            raise FetchError("Files are over 100 MB in all")
        files.append((row["name"], data))
    others = [f["name"] for f in rows if _printable(f["name"]) and f not in meshes]
    return (*bundle(files), others)


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
    def download(file_row):
        dl = _graphql(
            "mutation($id:ID!,$m:ID!){getDownloadLink(id:$id,printId:$m,fileType:stl,source:model_detail){ok output{link}}}",
            {"id": file_row["id"], "m": model_id})
        with open_url(dl["data"]["getDownloadLink"]["output"]["link"]) as resp:
            return read_capped(resp)
    return _download_all(data["data"]["model"]["stls"], download)


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
    def download(entry):  # ponytail: untested against the live API, no token yet.
        with open_url(entry["download_url"], headers=headers) as resp:
            return read_capped(resp)
    return _download_all(files, download)


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
