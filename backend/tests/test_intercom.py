import re

import pytest

from rental import flash, intercom
from rental.app import create_app
from rental.store import RentalStore
from test_accounts import OWNER, USER, issue, post, sign_in


STATION = '/intercom/station'
CALL = '/intercom/visits/1'
WEBM = b'\x1a\x45\xdf\xa3sample clip'
MP4 = b'\x00\x00\x00\x18ftypM4A test clip'


@pytest.fixture
def intercom_fixture(tmp_path, monkeypatch):
    now = [10000]
    monkeypatch.setattr(intercom, 'time', lambda: now[0])
    store = RentalStore(tmp_path / 'intercom.sqlite')
    store.initialize()
    with store.connect() as db:
        db.executemany('INSERT INTO houses(id,name) VALUES(?,?)', [(1, 'Nhà A'), (2, 'Nhà B')])
    store.register_device(1, 'doorbell-a')
    store.register_device(2, 'doorbell-b')

    def ring(visit_id, house_id=1):
        alias = 'a' if house_id == 1 else 'b'
        device = f'doorbell-{alias}'
        with store.write() as db:
            db.execute('''INSERT INTO visits(id,device_id,house_id,event_id,recipient_chat_id,jpeg,image_hash,received_at)
                          VALUES(?,?,?,?,'',X'00','test',?)''',
                       (visit_id, device, house_id, f'{device}-{visit_id:016x}', now[0]))

    app = create_app(store, OWNER)
    owner = app.test_client()
    assert sign_in(owner).status_code == 303
    return app, owner, ring, now


def make_code(owner, device='doorbell-a'):
    page = post(owner, f'/intercom/stations/{device}/pair', {})
    assert page.status_code == 200
    match = re.search(r'<output[^>]*>\s*([A-Z2-9]{8})\s*</output>', page.get_data(as_text=True))
    assert match
    return match[1]


def pair(station, code):
    station.get(STATION)
    with station.session_transaction() as data:
        csrf = data['station_csrf']
    return station.post(STATION + '/pair', data={'csrf': csrf, 'code': code})


def station_headers(station, visit_id):
    response = station.get(STATION)
    match = re.search(r'data-csrf="([^"]+)"', response.get_data(as_text=True))
    assert match
    return {'X-CSRF-Token': match[1], 'X-Intercom-Visit': str(visit_id)}


def operator_headers(owner):
    with owner.session_transaction() as data:
        return {'X-CSRF-Token': data['csrf']}


def test_intercom_setup_displays_incoming_call_until_it_expires(intercom_fixture):
    app, owner, ring, now = intercom_fixture
    station = app.test_client()
    assert pair(station, make_code(owner)).status_code == 303
    idle = owner.get('/intercom').get_data(as_text=True)
    assert 'data-intercom-incoming="/intercom/incoming"' in idle
    assert 'Chưa có lượt chuông mới' in idle
    assert CALL + '/answer' not in idle

    ring(1)
    assert CALL + '/answer' in owner.get('/intercom').get_data(as_text=True)
    now[0] += 15
    assert 'Trả lời ngay' in owner.get('/intercom').get_data(as_text=True)

    now[0] += intercom.RING_SECONDS
    assert CALL + '/answer' not in owner.get('/intercom').get_data(as_text=True)


def test_both_peers_exchange_audio_from_latest_ring(intercom_fixture):
    app, owner, ring, _ = intercom_fixture
    station = app.test_client()
    assert station.get(STATION + '/state').status_code == 401
    assert pair(station, make_code(owner)).status_code == 303
    assert station.get(STATION + '/state').json == {'status': 'idle', 'visit_id': None, 'clips': []}
    ring(1)
    assert station.get(STATION + '/state').json == {'status': 'ringing', 'visit_id': 1, 'clips': []}
    assert station.post(STATION + '/audio', headers=station_headers(station, 1),
                        data=WEBM, content_type='audio/webm').status_code == 409
    assert post(owner, CALL + '/answer', {}).location == CALL
    assert owner.get(CALL + '/state').json['status'] == 'active'
    assert station.get(STATION + '/state').json['status'] == 'active'
    headers = station_headers(station, 1)
    payload = station.post(STATION + '/audio', data=WEBM, content_type='audio/webm;codecs=opus', headers=headers)
    assert payload.status_code == 201
    guest_clip = owner.get(CALL + '/state').json['clips'][0]['url']
    assert station.get(STATION + '/state').json['clips'] == []
    assert station.get(STATION + '/clips/' + str(payload.json['id'])).status_code == 404
    assert owner.get(guest_clip).data == WEBM
    assert owner.get(guest_clip, headers={'Range': 'bytes=0-3'}).data == WEBM[:4]
    assert owner.get(guest_clip, headers={'Range': 'bytes=0-3'}).status_code == 206
    assert owner.get(guest_clip).headers['Cache-Control'] == 'no-store'
    voice = owner.post(CALL + '/audio', data=MP4, content_type='audio/mp4', headers=operator_headers(owner))
    assert voice.status_code == 201
    host_clip = station.get(STATION + '/state').json['clips'][0]['url']
    assert station.get(host_clip).data == MP4
    assert station.get(STATION + '/state', query_string={'after': voice.json['id']}).json['clips'] == []
    assert post(owner, CALL + '/end', {}).status_code == 303
    assert station.get(STATION + '/state').json['status'] == 'ended'
    assert station.get(host_clip).status_code == 404
    assert owner.get(guest_clip).status_code == 404


def test_call_page_keeps_the_flash_status_when_reopened(intercom_fixture, monkeypatch):
    app, owner, ring, now = intercom_fixture
    monkeypatch.setattr(flash, "time", lambda: now[0])
    store = app.extensions["rental_live"][0]
    station = app.test_client()
    assert pair(station, make_code(owner)).status_code == 303
    ring(1)
    assert post(owner, CALL + '/answer', {}).location == CALL
    assert "Mở cửa mô phỏng" in owner.get(CALL).get_data(as_text=True)

    assert post(owner, '/visits/1/flash', {}).status_code == 303
    page = owner.get(CALL).get_data(as_text=True)
    assert 'data-flash-status="/visits/1/flash"' in page
    assert "Đang chờ ESP32 xác nhận." in page
    assert "flash_status.js" in page
    command_id = flash.claim_flash(store, 'doorbell-a')
    assert flash.acknowledge_flash(store, 'doorbell-a', command_id)
    assert "ESP32 đã xác nhận bật đèn." in owner.get(CALL).get_data(as_text=True)

    ring(2)
    second_call = '/intercom/visits/2'
    assert post(owner, second_call + '/answer', {}).location == second_call
    assert post(owner, '/visits/2/flash', {}).status_code == 303
    now[0] += flash.QUEUE_SECONDS
    assert "Hết hạn chờ ESP32 xác nhận." in owner.get(second_call).get_data(as_text=True)


def test_station_code_single_use_clears_admin_and_can_be_revoked(intercom_fixture):
    app, owner, ring, now = intercom_fixture
    station = app.test_client()
    assert sign_in(station).status_code == 303
    code = make_code(owner)
    assert station.post(STATION + '/pair', data={'code': code}).status_code == 403
    assert pair(station, code).status_code == 303
    assert station.get('/').location.endswith('/login')
    another = app.test_client()
    assert pair(another, code).status_code == 303
    assert another.get(STATION + '/state').status_code == 401
    ring(1)
    post(owner, CALL + '/answer', {})
    assert station.get(STATION + '/state').json['status'] == 'active'
    assert post(owner, '/intercom/stations/doorbell-a/revoke', {}).status_code == 303
    assert station.get(STATION + '/state').status_code == 401
    assert owner.get(CALL + '/state').json['status'] == 'ended'
    code = make_code(owner)
    now[0] += intercom.PAIR_SECONDS
    assert pair(another, code).status_code == 303
    assert another.get(STATION + '/state').status_code == 401


def test_live_signaling_binds_offer_answer_to_current_peers(intercom_fixture):
    app, owner, ring, _ = intercom_fixture
    station = app.test_client()
    pair(station, make_code(owner))
    operator = 'a' * 32
    guest = 'b' * 32
    extra = 'c' * 32
    offer = {'type': 'offer', 'sdp': 'v=0\r\ns=offer\r\n'}
    answer = {'type': 'answer', 'sdp': 'v=0\r\ns=answer\r\n'}
    owner_headers = operator_headers(owner)
    door_headers = station_headers(station, 1)

    def host(**data):
        return owner.post(CALL + '/signal', headers=owner_headers, json={'client': operator, **data})

    def door(**data):
        return station.post(STATION + '/signal', headers=door_headers, json={'client': guest, **data})

    ring(1)
    assert door().status_code == 409
    assert post(owner, CALL + '/answer', {}).status_code == 303
    assert station.post(STATION + '/signal', json={'client': guest}).status_code == 403
    assert host().json == {'peer': None, 'description': None}
    assert host(target=guest, description=answer).status_code == 400
    assert door().json == {'peer': operator, 'description': None}
    assert host(target=guest, description=offer).status_code == 200
    assert door(client=extra).status_code == 409
    assert door().json == {'peer': operator, 'description': offer}
    assert door(target=operator, description=answer).status_code == 200
    assert host().json == {'peer': guest, 'description': answer}
    assert door(leave=True).status_code == 204
    assert host().json == {'peer': None, 'description': None}
    assert door(client=extra).json == {'peer': operator, 'description': None}
    assert post(owner, CALL + '/end', {}).status_code == 303
    assert host().status_code == 409
    assert door().status_code == 409


def test_intercom_scope_rejects_stale_visits_and_bad_audio(intercom_fixture):
    app, owner, ring, _ = intercom_fixture
    station_a = app.test_client()
    station_b = app.test_client()
    pair(station_a, make_code(owner, 'doorbell-a'))
    pair(station_b, make_code(owner, 'doorbell-b'))
    ring(1)
    ring(2, house_id=2)
    issue(owner, 'manager-b', 'manager', house_id=2)
    manager = app.test_client()
    assert sign_in(manager, 'manager-b', USER).status_code == 303
    assert post(manager, CALL + '/answer', {}).status_code == 404
    assert manager.get(CALL + '/state').status_code == 404
    assert post(manager, '/intercom/stations/doorbell-b/pair', {}).status_code == 403
    post(owner, CALL + '/answer', {})
    second_owner = app.test_client()
    assert sign_in(second_owner).status_code == 303
    assert second_owner.get(CALL + '/state').status_code == 409
    headers_a = station_headers(station_a, 1)
    assert station_b.post(STATION + '/audio', headers=station_headers(station_b, 1),
                          data=WEBM, content_type='audio/webm').status_code == 409
    assert station_a.post(STATION + '/audio', headers={'X-Intercom-Visit': '1'},
                          data=WEBM, content_type='audio/webm').status_code == 403
    assert station_a.post(STATION + '/audio', headers=headers_a,
                          data=b'<html>test page</html>', content_type='audio/webm').status_code == 415
    assert station_a.post(STATION + '/audio', headers=headers_a,
                          data=WEBM + b'0' * intercom.CLIP_BYTES, content_type='audio/webm').status_code == 413
    ring(3)
    post(owner, '/intercom/visits/3/answer', {})
    assert station_a.get(STATION + '/state').json['visit_id'] == 3
    assert station_a.post(STATION + '/audio', headers=headers_a,
                          data=WEBM, content_type='audio/webm').status_code == 409
    assert owner.get(CALL + '/state').json['status'] == 'ended'


def test_voice_memory_is_bounded_and_purged_at_expiry(monkeypatch):
    class ManualTimer:
        def __init__(self, seconds, callback):
            self.delay = seconds
            self.callback = callback

        def start(self):
            pass

        def cancel(self):
            pass

    now = [101.0]
    monkeypatch.setattr(intercom, 'monotonic', lambda: now[0])
    monkeypatch.setattr(intercom, 'Timer', ManualTimer)
    monkeypatch.setattr(intercom, 'MAX_AUDIO_BYTES', len(WEBM))
    memory = intercom.VoiceClips()
    clip_id = memory.save(1, 'station', 'audio/webm', WEBM)
    assert memory.peer_ids(1, 'operator', 0) == [clip_id]
    assert memory.save(2, 'station', 'audio/webm', WEBM) is None
    assert memory.timer.delay == intercom.CLIP_SECONDS
    now[0] += intercom.CLIP_SECONDS
    memory.timer.callback()
    assert not memory.clips and memory.size == 0


def test_hermes_assistant_activates_after_15s_logs_history_and_landlord_preempts(intercom_fixture, monkeypatch):
    from rental import app as rental_app, hermes_voice

    app, owner, ring, now = intercom_fixture
    monkeypatch.setattr(rental_app, 'time', lambda: now[0])
    monkeypatch.setattr(hermes_voice, 'time', lambda: now[0])
    monotonic_now = [100.0]
    monkeypatch.setattr(hermes_voice, 'monotonic', lambda: monotonic_now[0])
    monkeypatch.setenv('RENTAL_HERMES_URL', 'https://hermes.example/v1')
    monkeypatch.setenv('RENTAL_HERMES_KEY', 'k' * 32)
    monkeypatch.setenv('RENTAL_TELEGRAM_BOT_TOKEN', '123456:TESTTOKEN')

    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')

        def json(self):
            return self._payload

    class FakeHttp:
        def get(self, url, **kwargs):
            calls.append(('GET', url, kwargs))
            return FakeResponse(200, {'data': [{'name': 'web', 'enabled': False}]})

        def post(self, url, **kwargs):
            calls.append(('POST', url, kwargs))
            if url.endswith('/chat/completions'):
                assert kwargs['json']['tool_choice'] == 'none'
                return FakeResponse(200, {
                    'choices': [{'message': {'content': 'Chào bạn, tôi đã ghi nhận giao hàng phòng 01 và sẽ báo chủ trọ ngay.'}}]
                })
            if url.endswith('/sendMessage'):
                return FakeResponse(200, {'ok': True, 'result': {'message_id': 77}})
            raise AssertionError(f'Unexpected URL {url}')

    app.extensions['rental_assistant'].http = FakeHttp()
    # Set Telegram recipient on house 1 before ringing.
    assert post(owner, '/manage/telegram', {'house_id': '1', 'chat_id': '99887766'}).status_code == 303
    store = app.extensions['rental_live'][0]

    station = app.test_client()
    pair(station, make_code(owner, 'doorbell-a'))
    with store.write() as db:
        db.execute(
            """INSERT INTO visits(id,device_id,house_id,event_id,recipient_chat_id,jpeg,image_hash,received_at)
               VALUES(1,'doorbell-a',1,'doorbell-a-0000000000000001','99887766',X'00','test',?)""",
            (now[0],),
        )

    # Before 15 seconds: still ringing, assistant endpoint rejected.
    now[0] += 10
    state = station.get(STATION + '/state', query_string={'assistant': '1'}).json
    assert state['status'] == 'ringing'
    assert state['assistant_in'] == 5
    headers = station_headers(station, 1)
    assert station.post(STATION + '/assistant', headers=headers, json={'text': 'Xin chào'}).status_code == 409

    # At 15 seconds: automatically enters assistant mode.
    now[0] += 5
    state = station.get(STATION + '/state', query_string={'assistant': '1'}).json
    assert state['status'] == 'assistant'
    assert state['assistant_greeting']
    assert state['assistant_turns'] == 0
    incoming_html = owner.get('/intercom/incoming').get_data(as_text=True)
    assert 'Hermes đang tiếp khách' in incoming_html
    assert 'Trả lời ngay' in incoming_html

    # Empty message rejected; valid guest speech goes to Hermes and Telegram (no door flash queued).
    assert station.post(STATION + '/assistant', headers=headers, json={'text': '   '}).status_code == 400
    reply = station.post(STATION + '/assistant', headers=headers, json={'text': 'Mình giao hàng cho phòng 01'}).json
    assert 'giao hàng phòng 01' in reply['reply']
    assert reply['turn'] == 1
    assert reply['fallback'] is False
    assert reply['done'] is False

    # Verify Telegram got a text-only summary (no reply_markup/flash) and no flash command was created.
    tg_calls = [item for item in calls if item[1].endswith('/sendMessage')]
    assert len(tg_calls) == 1
    assert 'reply_markup' not in tg_calls[0][2]['data']
    assert 'Mình giao hàng cho phòng 01' in tg_calls[0][2]['data']['text']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM doorbell_flash_commands').fetchone()[0] == 0

    # Visit history dashboard displays the Hermes conversation transcript.
    dashboard_html = owner.get('/').get_data(as_text=True)
    assert 'Trợ lý Hermes:' in dashboard_html
    assert 'Mình giao hàng cho phòng 01' in dashboard_html

    # Rate limit blocks immediate duplicate turn; after interval passes, fallback works when Hermes fails.
    assert station.post(STATION + '/assistant', headers=headers, json={'text': 'Nhắn thêm'}).status_code == 429
    monotonic_now[0] += 2.0
    monkeypatch.delenv('RENTAL_HERMES_URL', raising=False)
    monkeypatch.delenv('RENTAL_HERMES_KEY', raising=False)
    fallback_reply = station.post(STATION + '/assistant', headers=headers, json={'text': 'Để trước cổng nhé'}).json
    assert fallback_reply['fallback'] is True
    assert fallback_reply['turn'] == 2

    # Landlord answers late at +25s -> immediately preempts Hermes and starts live WebRTC call.
    now[0] += 10
    assert post(owner, CALL + '/answer', {}).location == CALL
    assert station.get(STATION + '/state', query_string={'assistant': '1'}).json['status'] == 'active'
    assert station.post(STATION + '/assistant', headers=headers, json={'text': 'Alo'}).status_code == 409
    assert owner.post(CALL + '/signal', headers=operator_headers(owner), json={'client': 'a' * 32}).status_code == 200


def test_audio_fallback_transcribes_only_while_hermes_owns_the_call(intercom_fixture, monkeypatch):
    app, owner, ring, now = intercom_fixture
    monkeypatch.setenv('RENTAL_HERMES_URL', 'https://hermes.example/v1')
    monkeypatch.setenv('RENTAL_HERMES_KEY', 'test-key')
    monkeypatch.setenv('RENTAL_TELEGRAM_BOT_TOKEN', '')
    station = app.test_client()
    assert pair(station, make_code(owner)).status_code == 303
    ring(1)
    headers = station_headers(station, 1)
    assistant = app.extensions['rental_assistant']
    posts = []

    class FakeHttp:
        def post(self, url, **kwargs):
            assert url == 'https://hermes.example/v1/doorbell/transcriptions'
            assert kwargs['data'] == WEBM
            assert kwargs['headers'] == {'Authorization': 'Bearer test-key', 'Content-Type': 'audio/webm'}
            posts.append(url)

            class Reply:
                status_code = 200

                def json(self):
                    return {'text': 'Tôi giao hàng phòng 02.'}

            return Reply()

    assistant.http = FakeHttp()
    monkeypatch.setattr(assistant, '_ask_hermes', lambda house, history: f"Đã nhận. {history[-1]['text']}")
    endpoint = STATION + '/assistant'
    assert station.post(endpoint, headers=headers, data=WEBM, content_type='audio/webm').status_code == 409
    now[0] += 15
    assert station.post(endpoint, headers=headers, data=b'wrong clip', content_type='audio/webm').status_code == 415
    assert not posts

    result = station.post(endpoint, headers=headers, data=WEBM, content_type='audio/webm;codecs=opus')
    assert result.status_code == 200
    assert result.json['transcript'] == 'Tôi giao hàng phòng 02.'
    assert result.json['reply'] == 'Đã nhận. Tôi giao hàng phòng 02.'
    assert station.post(endpoint, headers=headers, data=WEBM, content_type='audio/webm').status_code == 429
    reply_id = result.json['reply_id']
    audio = b'RIFF' + bytes(4) + b'WAVE' + bytes(34)
    played = []

    def speech(turn, text):
        played.append((turn, text))
        return audio

    monkeypatch.setattr(assistant, 'synthesize_reply', speech)
    assert station.post(f'{endpoint}/speech/{reply_id + 1}', headers=headers).status_code == 404
    voice = station.post(f'{endpoint}/speech/{reply_id}', headers=headers)
    assert voice.status_code == 200
    assert voice.mimetype == 'audio/wav' and voice.data == audio
    assert voice.headers['Cache-Control'] == 'no-store'
    assert played == [(reply_id, result.json['reply'])]

    assert post(owner, CALL + '/answer', {}).location == CALL
    assert station.post(endpoint, headers=headers, data=WEBM, content_type='audio/webm').status_code == 409
    assert station.post(f'{endpoint}/speech/{reply_id}', headers=headers).status_code == 409
    assert len(posts) == 1


def test_single_house_flag_defaults_off_and_env_enables_day_a(tmp_path, monkeypatch):
    monkeypatch.delenv('RENTAL_SINGLE_HOUSE', raising=False)
    store_multi = RentalStore(tmp_path / 'multi.sqlite')
    store_multi.initialize()
    app_multi = create_app(store_multi, OWNER)
    assert app_multi.config['RENTAL_HOUSE_A_ID'] is None

    monkeypatch.setenv('RENTAL_SINGLE_HOUSE', '1')
    store_single = RentalStore(tmp_path / 'single.sqlite')
    store_single.initialize()
    app_single = create_app(store_single, OWNER)
    assert app_single.config['RENTAL_HOUSE_A_ID'] is not None
