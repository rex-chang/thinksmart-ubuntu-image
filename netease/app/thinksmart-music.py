#!/opt/thinksmart-addons/venv/bin/python
"""ThinkSmart NetEase music app (touch UI on http://127.0.0.1:8091/).

Thin loopback service: serves the self-contained touch page and proxies its API calls to
the hub's loopback NetEase bridge (/kiosk/netease/music/* and /kiosk/netease/login/* on the
hub admin port). All playback state lives in the hub (plugins_func/netease + mpv), so the
voice assistant, the home music card and this app all drive the same queue.
"""
import os
from pathlib import Path

import aiohttp
from aiohttp import web

PORT = 8091
SHARE = Path(os.environ.get('MUSIC_SHARE', '/usr/local/share/thinksmart-music'))
HUB = os.environ.get('HUB_API', 'http://127.0.0.1:16688/kiosk/netease')
SESSION = web.AppKey('session', aiohttp.ClientSession)
ALLOWED_HOSTS = {'127.0.0.1:8091', 'localhost:8091'}
ALLOWED_ORIGINS = {f'http://{h}:8091' for h in ('127.0.0.1', 'localhost')}

GET_PATHS = {'status', 'lyrics', 'search', 'playlists', 'playlist_tracks', 'queue'}
POST_PATHS = {'seek', 'volume', 'control', 'play_queue', 'play_index', 'like'}
LOGIN_GET = {'status'}
LOGIN_POST = {'start', 'refresh', 'cancel', 'logout'}


@web.middleware
async def guard(request, handler):
    if request.remote not in ('127.0.0.1', '::1'):
        return web.Response(status=403, text='loopback only')
    if request.host not in ALLOWED_HOSTS:
        return web.Response(status=403, text='bad host')
    origin = request.headers.get('Origin')
    if origin and origin not in ALLOWED_ORIGINS:
        return web.Response(status=403, text='bad origin')
    return await handler(request)


async def index(_request):
    data = (SHARE / 'index.html').read_bytes()
    return web.Response(body=data, content_type='text/html', charset='utf-8',
                        headers={'Cache-Control': 'no-store'})


async def proxy_get(request):
    tail = request.path[len('/api/music/'):] if request.path.startswith('/api/music/') else ''
    if request.path.startswith('/api/login/'):
        tail = request.path[len('/api/login/'):]
        if tail not in LOGIN_GET:
            raise web.HTTPNotFound()
        url = f'{HUB}/login/{tail}'
    elif tail in GET_PATHS:
        url = f'{HUB}/music/{tail}'
    else:
        raise web.HTTPNotFound()
    async with request.app[SESSION].get(url, params=request.query,
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
        raw = await r.content.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise web.HTTPBadGateway()
        return web.Response(status=r.status, body=raw,
                            content_type=r.content_type or 'application/json', charset='utf-8')


async def proxy_post(request):
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType()
    tail = request.path[len('/api/music/'):] if request.path.startswith('/api/music/') else ''
    if request.path.startswith('/api/login/'):
        tail = request.path[len('/api/login/'):]
        if tail not in LOGIN_POST:
            raise web.HTTPNotFound()
        # the hub's logout route sits at /kiosk/netease/logout, without the /login/ segment
        url = f'{HUB}/logout' if tail == 'logout' else f'{HUB}/login/{tail}'
    elif tail in POST_PATHS:
        url = f'{HUB}/music/{tail}'
    else:
        raise web.HTTPNotFound()
    raw = await request.content.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise web.HTTPRequestEntityTooLarge()
    async with request.app[SESSION].post(url, data=raw, headers={'Content-Type': 'application/json'},
                                         timeout=aiohttp.ClientTimeout(total=25)) as r:
        body = await r.content.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise web.HTTPBadGateway()
        return web.Response(status=r.status, body=body,
                            content_type=r.content_type or 'application/json', charset='utf-8')


def make_app():
    app = web.Application(middlewares=[guard])

    async def lifecycle(app_):
        app_[SESSION] = aiohttp.ClientSession(trust_env=False)
        try:
            yield
        finally:
            await app_[SESSION].close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', index)
    app.router.add_get('/api/music/{tail}', proxy_get)
    app.router.add_get('/api/login/{tail}', proxy_get)
    app.router.add_post('/api/music/{tail}', proxy_post)
    app.router.add_post('/api/login/{tail}', proxy_post)
    return app


if __name__ == '__main__':
    web.run_app(make_app(), host='127.0.0.1', port=PORT, access_log=None, print=None)
