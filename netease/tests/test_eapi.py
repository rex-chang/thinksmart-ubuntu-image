import hashlib
import json
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from plugins_func.netease.eapi import EAPI_KEY, eapi_encrypt

SEP = "-36cd479b6b5-"


def decrypt(hexstr):
    d = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).decryptor()
    raw = d.update(bytes.fromhex(hexstr)) + d.finalize()
    pad = raw[-1]
    return raw[:-pad].decode()


class TestEncrypt(unittest.TestCase):
    def test_layout_and_digest(self):
        path = "/api/song/enhance/player/url/v1"
        data = {"ids": "[1]", "level": "exhigh", "header": {"os": "pc"}, "e_r": False}
        plain = decrypt(eapi_encrypt(path, data))
        self.assertTrue(plain.startswith(path + SEP))
        body = plain.split(SEP)[1]
        digest = plain.split(SEP)[2]
        self.assertEqual(json.loads(body), data)
        self.assertEqual(
            digest,
            hashlib.md5(
                ("nobody" + path + "use" + body + "md5forencrypt").encode()
            ).hexdigest(),
        )

    def test_padding_multiple_of_block(self):
        # 加密结果按 16 字节分组，长度必为 32 个 hex 的倍数
        out = eapi_encrypt("/api/x", {"a": 1})
        self.assertEqual(len(out) % 32, 0)


if __name__ == "__main__":
    unittest.main()
