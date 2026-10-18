"""Retire failed playlist URLs across metadata copies, not newer cached URLs."""

import pytest

from tests.test_playlist_watch import MEDIA, _channel_data, _make_channel, _playlist, _Transport


FRESH = 'https://video.example/fresh/playlist.m3u8'
SEGMENT = 'https://video.example/fresh/segment.ts'
STALE_SEGMENT = 'https://video.example/live/stale.ts'
SEEN = 'https://video.example/live/seen.ts'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 404])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('replacement', ['copied', 'fresh', 'other_broadcast'])
async def test_failed_url_invalidation_preserves_newer_cache_and_recovers(status, method, replacement):
    transport = _Transport()
    master = f'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\n{FRESH}\n'
    channel = _make_channel(transport, master_body=master)
    original = channel._stream
    channel._watch_broadcast_id = original.broadcast_id
    channel._watched_segments[SEEN] = None
    channel._last_spade_sent = 1.0

    def replace_metadata():
        channel.external_update(_channel_data(457 if replacement == 'other_broadcast' else 456), [])
        if replacement != 'copied':
            channel._stream._stream_url = FRESH

    if method == 'GET':
        transport.add('GET', MEDIA, status=status, on_enter=replace_metadata)
    else:
        transport.add('GET', MEDIA, body=_playlist(STALE_SEGMENT))
        transport.add('HEAD', STALE_SEGMENT, status=status, on_enter=replace_metadata)
    transport.add('GET', FRESH, body=_playlist(SEGMENT))
    transport.add('HEAD', SEGMENT)

    assert await channel._send_watch_playlist() is False

    current = channel._stream
    assert current is not original
    assert current._stream_url == (None if replacement == 'copied' else FRESH)
    assert channel._last_spade_sent == 1.0
    assert SEEN in channel._watched_segments
    assert await channel._send_watch_playlist() is True
    assert transport.requests.count(('GET', MEDIA)) == 1
    assert transport.heads[-1] == SEGMENT
    assert channel._twitch.gql_request.await_count == (1 if replacement == 'copied' else 0)
    assert (SEEN in channel._watched_segments) is (replacement != 'other_broadcast')
    assert channel.online
    assert all(method != 'POST' and (method != 'GET' or not url.endswith('.ts'))
               for method, url in transport.requests)
    assert transport.closed == transport.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_unrelated_http_failure_keeps_playlist_cache(method):
    transport = _Transport()
    channel = _make_channel(transport)
    if method == 'GET':
        transport.add('GET', MEDIA, status=500)
    else:
        transport.add('GET', MEDIA, body=_playlist(STALE_SEGMENT))
        transport.add('HEAD', STALE_SEGMENT, status=500)

    assert await channel._send_watch_playlist() is False
    assert channel._stream._stream_url == MEDIA
    channel._twitch.gql_request.assert_not_awaited()
