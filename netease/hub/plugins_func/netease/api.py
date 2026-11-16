from dataclasses import dataclass, field
import json

from . import eapi

API_SUCCESS = 200
NETWORK_RISK_CODE = -460

LEVELS = ["standard", "higher", "exhigh", "lossless", "hires"]


@dataclass
class Privilege:
    st: int = 0
    pl: int = 0


@dataclass
class Track:
    id: int
    name: str
    artists: list = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    fee: int = 0
    privilege: Privilege | None = None
    cover: str = ""


@dataclass
class Stream:
    url: str
    type: str = "mp3"
    level: str = "standard"
    size: int = 0


def _track(raw):
    p = raw.get("privilege")
    return Track(
        raw.get("id", 0),
        raw.get("name", ""),
        [a.get("name", "") for a in raw.get("ar") or []],
        (raw.get("al") or {}).get("name", ""),
        raw.get("dt", 0),
        raw.get("fee", 0),
        Privilege(p.get("st", 0), p.get("pl", 0)) if p else None,
        (raw.get("al") or {}).get("picUrl", ""),
    )


def playable(t: Track) -> bool:
    if t.privilege:
        return t.privilege.st >= 0 and t.privilege.pl > 0
    return t.fee not in (1, 4)


def search_songs(client, q, limit=10):
    res = client.web_get(
        "/api/cloudsearch/pc",
        {"s": q, "type": 1, "limit": limit, "offset": 0, "total": "true"},
    )
    songs = (res.get("result") or {}).get("songs") or []
    return [_track(s) for s in songs]


def lyrics(client, song_id):
    res = client.web_get("/api/song/lyric",
                         {"id": song_id, "lv": -1, "kv": -1, "tv": -1})
    return ((res.get("lrc") or {}).get("lyric") or "",
            (res.get("tlyric") or {}).get("lyric") or "")


def daily_songs(client):
    res = client.web_get("/api/v3/discovery/recommend/songs")
    return [_track(s) for s in ((res.get("data") or {}).get("dailySongs") or [])]


def user_playlists(client, uid):
    res = client.web_get("/api/user/playlist", {"uid": uid, "limit": 200, "offset": 0})
    return res.get("playlist") or []


def song_details(client, ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        c = json.dumps([{"id": x} for x in chunk], separators=(",", ":"))
        res = client.eapi_post("/api/v3/song/detail", {"c": c})
        for s in res.get("songs") or []:
            out[s.get("id")] = _track(s)
        for p in res.get("privileges") or []:
            t = out.get(p.get("id"))
            if t:
                t.privilege = Privilege(p.get("st", 0), p.get("pl", 0))
    return out


def playlist_tracks(client, pid):
    res = client.web_get("/api/v6/playlist/detail", {"id": pid, "n": 1000})
    pl = res.get("playlist") or {}
    tracks = [_track(s) for s in pl.get("tracks") or []]
    if not tracks and pl.get("trackIds"):
        ids = [x.get("id") for x in pl["trackIds"] if x.get("id")]
        details = song_details(client, ids)
        tracks = [details[i] for i in ids if i in details]
    return tracks


def _lower(level):
    i = LEVELS.index(level) if level in LEVELS else 2
    return LEVELS[:i + 1][::-1]


_WEB_BR = {"standard": 128000, "higher": 192000, "exhigh": 320000,
           "lossless": 999000, "hires": 1999000}


def _stream_from(item, lv):
    if item.get("freeTrialInfo"):
        raise LookupError("unavailable")
    if not item.get("url"):
        return None
    return Stream(
        item["url"].replace("http://", "https://", 1),
        (item.get("type") or item.get("encodeType") or "mp3").lower(),
        item.get("level") or lv,
        item.get("size") or 0,
    )


def _song_url_eapi(client, track_id, levels):
    for lv in levels:
        res = client.eapi_post(
            "/api/song/enhance/player/url/v1",
            {
                "ids": f"[{track_id}]",
                "level": lv,
                "encodeType": "flac" if lv in ("lossless", "hires") else "mp3",
            },
        )
        code = res.get("code")
        if code not in (None, API_SUCCESS):
            raise eapi.NeteaseError(code)
        s = _stream_from((res.get("data") or [{}])[0], lv)
        if s:
            return s
    raise LookupError("unavailable")


def _song_url_web(client, track_id, levels):
    """Risk-control fallback (-460): the same session over the plain web endpoint still
    resolves stream URLs when the PC-client transport is rejected; the server picks the
    actual quality. Returns None-shaped LookupError when nothing resolves."""
    import requests

    for lv in levels:
        r = requests.post(
            eapi.WEB_BASE + "/api/song/enhance/player/url",
            data={"ids": f"[{track_id}]", "br": _WEB_BR.get(lv, 320000), "csrf": client.csrf},
            headers={"User-Agent": eapi.UA_WEB, "Cookie": client.cookie_header()},
            timeout=15,
        )
        try:
            d = r.json()
        except ValueError:
            continue
        if d.get("code") not in (None, API_SUCCESS):
            continue
        s = _stream_from((d.get("data") or [{}])[0], lv)
        if s:
            return s
    raise LookupError("unavailable")


def song_url(client, track_id, level="exhigh"):
    levels = _lower(level)
    try:
        return _song_url_eapi(client, track_id, levels)
    except eapi.NeteaseError as e:
        if e.code != NETWORK_RISK_CODE:
            raise
    return _song_url_web(client, track_id, levels)


def like(client, track_id, like=True):
    res = client.eapi_post(
        "/api/radio/like",
        {
            "alg": "itembased",
            "trackId": str(track_id),
            "like": "true" if like else "false",
            "time": "3",
        },
    )
    code = res.get("code")
    if code not in (None, 200):
        raise LookupError(f"like failed: {code}")
