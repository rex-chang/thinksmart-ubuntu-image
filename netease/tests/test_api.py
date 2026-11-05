import unittest

from plugins_func.netease import api


class Fake:
    def __init__(self, web=None, eapi=None):
        self.web = web or {}
        self.eapi = list(eapi or [])
        self.posted = []

    def web_get(self, path, params=None):
        return self.web[path]

    def eapi_post(self, path, data):
        self.posted.append((path, data))
        if path == "/api/song/enhance/player/url/v1":
            return self.eapi.pop(0)
        return {"code": 200}


SONG = {
    "id": 1,
    "name": "稻香",
    "ar": [{"name": "周杰伦"}],
    "al": {"name": "魔杰座"},
    "dt": 223000,
    "fee": 0,
    "privilege": {"st": 0, "pl": 320000},
}


class TestApi(unittest.TestCase):
    def test_search_maps_track(self):
        c = Fake(web={"/api/cloudsearch/pc": {"result": {"songs": [SONG]}}})
        t = api.search_songs(c, "稻香")[0]
        self.assertEqual((t.id, t.name, t.artists, t.album), (1, "稻香", ["周杰伦"], "魔杰座"))
        self.assertTrue(api.playable(t))

    def test_vip_track_is_not_playable(self):
        t = api._track({**SONG, "fee": 1, "privilege": {"st": 0, "pl": 0}})
        self.assertFalse(api.playable(t))

    def test_song_url_falls_back_level(self):
        c = Fake(eapi=[
            {"data": [{"url": "", "freeTrialInfo": None}]},
            {"data": [{"url": "http://x/1.mp3", "type": "mp3", "level": "exhigh",
                       "size": 10, "freeTrialInfo": None}]},
        ])
        s = api.song_url(c, 1, "lossless")
        self.assertEqual(s.url, "https://x/1.mp3")
        self.assertEqual(s.level, "exhigh")
        self.assertEqual(len(c.posted), 2)

    def test_song_url_rejects_preview(self):
        c = Fake(eapi=[{"data": [{"url": "http://x/preview.mp3", "freeTrialInfo": {"start": 0}}]}])
        with self.assertRaises(LookupError):
            api.song_url(c, 1, "exhigh")

    def test_like_payload(self):
        c = Fake()
        api.like(c, 7, True)
        path, data = c.posted[-1]
        self.assertEqual(path, "/api/radio/like")
        self.assertEqual(data["trackId"], "7")
        self.assertEqual(data["like"], "true")


if __name__ == "__main__":
    unittest.main()
