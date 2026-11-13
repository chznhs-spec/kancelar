import hashlib
import tempfile
import unittest
from pathlib import Path

import vlcr


class VLCRTests(unittest.TestCase):
    def test_round_trip_plain(self):
        raw = b"hello vlcr"
        packed = vlcr.encode_bytes(raw, original_name="hello.txt", media_type="text/plain")
        doc = vlcr.decode_bytes(packed)
        self.assertEqual(doc.data, raw)
        self.assertEqual(doc.media_type, "text/plain")
        self.assertEqual(doc.original_name, "hello.txt")

    def test_round_trip_compressed(self):
        raw = b"A" * 100_000
        packed = vlcr.encode_bytes(raw, original_name="repeat.txt", media_type="text/plain", compress=True)
        doc = vlcr.decode_bytes(packed)
        self.assertTrue(doc.compressed)
        self.assertEqual(doc.data, raw)

    def test_corruption_is_detected(self):
        packed = bytearray(vlcr.encode_bytes(b"original", original_name="a.bin"))
        packed[-1] ^= 1
        with self.assertRaises(vlcr.VLCRIntegrityError):
            vlcr.decode_bytes(packed)

    def test_wrong_magic_is_rejected(self):
        packed = bytearray(vlcr.encode_bytes(b"x"))
        packed[0:4] = b"NOPE"
        with self.assertRaises(vlcr.VLCRFormatError):
            vlcr.decode_bytes(packed)

    def test_pack_unpack_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            source.write_text("VLCR test", encoding="utf-8")
            container = vlcr.pack_file(source)
            restored = vlcr.unpack_file(container, root / "out")
            self.assertEqual(restored.read_text(encoding="utf-8"), "VLCR test")

    def test_extract_uses_basename_only(self):
        packed = vlcr.encode_bytes(b"x", original_name="../../outside.txt", media_type="text/plain")
        doc = vlcr.decode_bytes(packed)
        self.assertEqual(doc.original_name, "outside.txt")


if __name__ == "__main__":
    unittest.main()
