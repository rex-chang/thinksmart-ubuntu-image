import unittest

from plugins_func.netease.player import Player


class FakeMpv:
    def __init__(self):
        self.cmds = []
        self.on_event = None

    def command(self, *args):
        self.cmds.append(args)

    def get_property(self, name):
        return 12.5


def track(i):
    return type("T", (), {"id": i, "name": f"s{i}", "artists": ["a"]})()


def resolver(tid, level):
    class S:
        url = f"http://cdn/{tid}.mp3"

    return S()


def failing_for(bad_id):
    def fn(tid, level):
        if tid == bad_id:
            raise LookupError("unavailable")
        return resolver(tid, level)

    return fn


class TestQueue(unittest.TestCase):
    def make(self, fn=resolver):
        mpv = FakeMpv()
        return mpv, Player(mpv, fn)

    def test_eof_advances_and_stops_at_end(self):
        _, p = self.make()
        p.play_queue([track(1), track(2)])
        self.assertEqual(p.current.id, 1)
        p.on_event("end-file", {"reason": "eof"})
        self.assertEqual(p.current.id, 2)
        p.on_event("end-file", {"reason": "eof"})
        self.assertIsNone(p.current)

    def test_repeat_all_wraps(self):
        _, p = self.make()
        p.play_queue([track(1), track(2)])
        p.set_repeat("all")
        p.on_event("end-file", {"reason": "eof"})
        p.on_event("end-file", {"reason": "eof"})
        self.assertEqual(p.current.id, 1)

    def test_error_skips_unavailable_track(self):
        _, p = self.make(failing_for(1))
        p.play_queue([track(1), track(2)])
        self.assertEqual(p.current.id, 2)

    def test_abort_pauses_only_once(self):
        mpv, p = self.make()
        p.play_queue([track(1)])
        p.pause_for_abort()
        p.pause_for_abort()
        self.assertEqual(mpv.cmds[-1], ("set_property", "pause", True))
        self.assertEqual(sum(1 for c in mpv.cmds if c == ("set_property", "pause", True)), 1)

    def test_status_has_position(self):
        _, p = self.make()
        p.play_queue([track(1)])
        st = p.status()
        self.assertEqual(st["name"], "s1")
        self.assertEqual(st["position"], 12.5)


if __name__ == "__main__":
    unittest.main()
