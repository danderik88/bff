"""Send a sliced file to a Bambu printer over implicit FTPS (port 990)."""
import ftplib
import io
import socket
import ssl


class UploadError(Exception):
    pass


class ImplicitFTP(ftplib.FTP_TLS):
    """TLS from the first byte, on the data channel too, reusing the control session (the A1 drops it otherwise)."""

    def __init__(self, check_cert=None, timeout=60):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Self-signed: pinned by check_cert.
        super().__init__(context=ctx, timeout=timeout)
        self.check_cert = check_cert

    def connect(self, host="", port=990, timeout=-999, source_address=None):
        self.host, self.port = host, port
        sock = socket.create_connection((host, port), self.timeout)
        self.sock = self.context.wrap_socket(sock, server_hostname=host)
        if self.check_cert:
            self.check_cert(self.sock.getpeercert(binary_form=True))
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        """ftplib's, minus the TLS unwrap: the A1 never answers it, so the transfer would time out."""
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
        return self.voidresp()


def upload(host, access_code, name, data, check_cert=None, ftp_class=ImplicitFTP):
    """Put `data` at /<name>; raise UploadError unless the printer holds exactly that many bytes."""
    try:
        ftp = ftp_class(check_cert=check_cert)
        ftp.connect(host, 990)
        try:
            ftp.login("bblp", access_code)
            ftp.prot_p()
            stored = True
            try:
                ftp.storbinary(f"STOR /{name}", io.BytesIO(data))
            except ftplib.Error:
                stored = False  # Some firmwares answer the end of transfer badly: the size below decides.
            try:
                size = ftp.size(f"/{name}")
            except ftplib.error_perm:
                if stored:
                    raise
                # Bravo 2026-11-15: empty root, the file vanished after STOR = no SD card seen
                raise UploadError("memoria della stampante non disponibile: inserisci una chiavetta USB o una scheda SD") from None
        finally:
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()
    except (OSError, ftplib.Error, EOFError) as err:
        raise UploadError(f"invio del file non riuscito: {err or type(err).__name__}") from err
    if size != len(data):
        raise UploadError(f"file incompleto sulla stampante ({size} di {len(data)} byte)")
