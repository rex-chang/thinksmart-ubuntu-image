import hashlib
import json
import random
import time
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import session

WEB_BASE = "https://music.163.com"
EAPI_BASE = "https://interfacepc.music.163.com"
EAPI_KEY = b"e82ckenh8dichen8"
UA_WEB = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
UA_EAPI = "NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)"


class NeteaseError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"netease code {code}: {msg}")
        self.code = code


class LoginRequired(NeteaseError):
    pass


def eapi_encrypt(path: str, data: dict) -> str:
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(
        ("nobody" + path + "use" + body + "md5forencrypt").encode()
    ).hexdigest()
    plain = f"{path}-36cd479b6b5-{body}-36cd479b6b5-{digest}".encode()
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).encryptor()
    return (enc.update(plain) + enc.finalize()).hex()


class Client:
    def __init__(self):
        s = session.load() or {}
        self.music_u = s.get("music_u", "")
        self.csrf = s.get("csrf", "")
        self.nmtid = s.get("nmtid", "")
        self.device = session.device_id()

    def _header(self):
        h = {
            "osver": "Microsoft-Windows-10-Professional-build-19045-64bit",
            "deviceId": self.device,
            "os": "pc",
            "appver": "3.1.17.204416",
            "versioncode": "140",
            "mobilename": "",
            "buildver": str(int(time.time())),
            "resolution": "1920x1080",
            "channel": "netease",
            "requestId": f"{int(time.time() * 1000)}_{random.randrange(1000):04d}",
            "__csrf": self.csrf,
        }
        if self.music_u:
            h["MUSIC_U"] = self.music_u
        if self.nmtid:
            h["NMTID"] = self.nmtid
        return h

    def cookie_header(self):
        return "; ".join(
            f"{k}={quote(str(v), safe='')}" for k, v in self._header().items()
        )

    def _absorb(self, resp):
        for c in resp.cookies:
            if c.name == "MUSIC_U":
                self.music_u = c.value
            elif c.name == "__csrf":
                self.csrf = c.value
            elif c.name == "NMTID":
                self.nmtid = c.value

    def _json(self, resp, check=True):
        if resp.status_code == 401:
            raise LoginRequired(401)
        try:
            data = resp.json()
        except ValueError:
            raise NeteaseError(resp.status_code, resp.text[:80])
        code = data.get("code")
        if code in (301, 302):
            raise LoginRequired(code, data.get("message") or data.get("msg") or "")
        if check and code not in (None, 200):
            raise NeteaseError(code, data.get("message") or data.get("msg") or "")
        return data

    def web_get(self, path, params=None):
        r = requests.get(
            WEB_BASE + path,
            params=params,
            timeout=15,
            headers={"User-Agent": UA_WEB, "Cookie": self.cookie_header()},
        )
        self._absorb(r)
        return self._json(r)

    def eapi_post(self, path, data):
        header = self._header()
        payload = dict(data, header=header, e_r=False)
        r = requests.post(
            EAPI_BASE + "/eapi/" + path[len("/api/"):],
            data={"params": eapi_encrypt(path, payload)},
            timeout=15,
            headers={
                "User-Agent": UA_EAPI,
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": self.cookie_header(),
            },
        )
        self._absorb(r)
        # 登录轮询会返回 801/802/803，这里不做 code==200 校验
        return self._json(r, check=False)

    def account(self):
        return self.web_get("/api/nuser/account/get").get("profile")
