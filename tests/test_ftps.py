"""FTPS upload and the project_file payload that starts it. Run: python3 -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pygmy"))
import ftps  # noqa: E402
from printer_link import project_file  # noqa: E402


class FakeFTP:
    """Records what upload() asks of the printer; size() reports what actually landed."""

    def __init__(self, check_cert=None):
        self.check_cert = check_cert
        self.user = None
        self.password = None
        self.stored = {}
        self.size_offset = 0

    def connect(self, host, port):
        pass

    def login(self, user, password):
        self.user, self.password = user, password

    def prot_p(self):
        pass

    def storbinary(self, cmd, fp):
        self.stored[cmd.removeprefix("STOR ")] = fp.read()

    def size(self, path):
        return len(self.stored.get(path, b"")) + self.size_offset

    def quit(self):
        pass

    def close(self):
        pass


class UploadTest(unittest.TestCase):
    def test_upload_puts_file_in_root_and_checks_size(self):
        made = []

        def factory(**kwargs):
            made.append(FakeFTP(**kwargs))
            return made[-1]

        ftps.upload("h", "code", "x.gcode.3mf", b"payload", ftp_class=factory)
        self.assertEqual(made[0].user, "bblp")
        self.assertEqual(made[0].password, "code")
        self.assertEqual(made[0].stored, {"/x.gcode.3mf": b"payload"})

    def test_size_mismatch_raises(self):
        def factory(**kwargs):
            ftp = FakeFTP(**kwargs)
            ftp.size_offset = -1
            return ftp

        with self.assertRaises(ftps.UploadError):
            ftps.upload("h", "code", "x.gcode.3mf", b"payload", ftp_class=factory)

    def test_interrupted_upload_raises(self):
        class Broken(FakeFTP):
            def storbinary(self, cmd, fp):
                raise OSError("broken pipe")

        with self.assertRaises(ftps.UploadError):
            ftps.upload("h", "code", "x.gcode.3mf", b"payload", ftp_class=Broken)

    def test_no_storage_says_check_the_sd_card(self):
        # Bravo (P2S) without a USB stick: STOR fails, then SIZE answers 550
        import ftplib

        class NoStorage(FakeFTP):
            def storbinary(self, cmd, fp):
                raise ftplib.error_perm("553 Could not create file.")

            def size(self, path):
                raise ftplib.error_perm("550 Could not get file size.")

        with self.assertRaisesRegex(ftps.UploadError, "chiavetta USB o una scheda SD"):
            ftps.upload("h", "code", "x.gcode.3mf", b"payload", ftp_class=NoStorage)

    def test_size_refused_after_a_good_store_stays_generic(self):
        import ftplib

        class NoSize(FakeFTP):
            def size(self, path):
                raise ftplib.error_perm("550 Could not get file size.")

        with self.assertRaisesRegex(ftps.UploadError, "invio del file non riuscito"):
            ftps.upload("h", "code", "x.gcode.3mf", b"payload", ftp_class=NoSize)

    def test_project_file_uses_spool_holder(self):
        print_section = project_file("a.gcode.3mf")["print"]
        self.assertEqual(print_section["command"], "project_file")
        self.assertEqual(print_section["url"], "ftp://a.gcode.3mf")
        self.assertEqual(print_section["param"], "Metadata/plate_1.gcode")
        self.assertEqual(print_section["ams_mapping"], [-1])
        self.assertEqual(print_section["ams_mapping2"], [{"ams_id": 255, "slot_id": 0}])
        self.assertIs(print_section["use_ams"], False)

    def test_project_file_keeps_a_gap_for_an_unused_filament(self):
        print_section = project_file("a.gcode.3mf", (0, None, 2))["print"]
        self.assertEqual(print_section["ams_mapping"], [0, -1, 2])
        self.assertEqual(print_section["ams_mapping2"],
                         [{"ams_id": 0, "slot_id": 0}, {"ams_id": 255, "slot_id": 255}, {"ams_id": 0, "slot_id": 2}])
        self.assertIs(print_section["use_ams"], True)


if __name__ == "__main__":
    unittest.main()
