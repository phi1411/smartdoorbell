import json
from time import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from rental import telegram_controls
from rental.flash import acknowledge_flash, claim_flash, initialize_flash, queue_direct_flash, queue_telegram_flash
from rental.store import RentalStore, digest
from rental.telegram_controls import DoorMenu, poll_callbacks, respond_to_callback
from rental.worker import VisitWorker


API_BASE = 'https://telegram.invalid/bot-test'


def make_callback(*, chat_id=12345, sender_id=12345, chat_type='private', data='flash:11'):
    return {'id': 'callback-1', 'data': data,
            'from': {'id': sender_id, 'is_bot': False},
            'message': {'message_id': 9, 'chat': {'type': chat_type, 'id': chat_id}}}


def test_photo_button_only_appears_in_private_chat_after_caption_edit():
    http = Mock()
    http.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 9}})
    worker = VisitWorker(None, http)
    for chat_id in ('12345', '-10012345'):
        job = {'id': 11, 'name': 'Dãy A', 'received_at': 0, 'jpeg': b'jpeg',
               'recipient_chat_id': chat_id, 'telegram_message_id': 9}
        result = worker.notify(job, 'Nhận diện')
        assert result == ('sent', 9)
        assert http.post.call_args.args[0].endswith('/sendPhoto')
        data = http.post.call_args.kwargs['data']
        if chat_id == '12345':
            assert json.loads(data['reply_markup']) == {
                'inline_keyboard': [[{'text': 'Mở cửa mô phỏng', 'callback_data': 'flash:11'}]],
            }
        else:
            assert 'reply_markup' not in data
        worker.update_caption(job, 'Nhận diện\nHermes: Có khách.')
        assert http.post.call_args.args[0].endswith('/editMessageCaption')
        edited = http.post.call_args.kwargs['data']
        assert edited['message_id'] == 9 and edited['chat_id'] == chat_id
        assert edited.get('reply_markup') == data.get('reply_markup')


@pytest.mark.parametrize('callback', [
    make_callback(chat_type='supergroup'),
    make_callback(chat_id=-100123, sender_id=-100123),
    make_callback(sender_id=9876),
    make_callback(data='flash:9999999999999999999'),
    make_callback(data='open'),
    {'id': 'callback-1', 'inline_message_id': 'message-1', 'data': 'flash:11', 'from': {'id': 12345}},
])
def test_untrusted_callback_cannot_queue_command(callback):
    queue = Mock()
    http = Mock()
    respond_to_callback(http, API_BASE, callback, queue)
    queue.assert_not_called()
    answer = http.post.call_args.kwargs['json']
    assert answer['show_alert'] is True
    assert 'không có quyền' in answer['text']


@pytest.mark.parametrize(('state', 'expected'), [('queued', 'Đang chờ'), ('confirmed', 'đã xác nhận')])
def test_private_button_forwards_original_chat_and_message_with_accurate_status(state, expected):
    queue = Mock(return_value=state)
    http = Mock()
    respond_to_callback(http, API_BASE, make_callback(), queue)
    queue.assert_called_once_with(11, '12345', 9)
    assert http.post.call_args.args[0].endswith('/answerCallbackQuery')
    answer = http.post.call_args.kwargs['json']
    assert answer['show_alert'] is False
    assert answer['cache_time'] == 0
    assert expected in answer['text']


def test_original_telegram_photo_can_trigger_only_one_confirmed_pulse(tmp_path, monkeypatch):
    for name, value in {
        'RENTAL_FACE_EMBED_URL': '', 'RENTAL_FACE_API_URL': '', 'RENTAL_HERMES_URL': '',
        'RENTAL_HERMES_KEY': '', 'RENTAL_TELEGRAM_BOT_TOKEN': 'test-bot-token',
    }.items():
        monkeypatch.setenv(name, value)
    store = RentalStore(tmp_path / 'telegram.sqlite')
    store.initialize()
    initialize_flash(store)
    with store.connect() as db:
        db.execute("INSERT INTO houses(id,name,telegram_chat_id) VALUES(1,'Dãy A','12345')")
    token = store.register_device(1, 'doorbell-test')
    payload = b'test-jpeg'
    assert store.receive('doorbell-test', digest(token.encode()), 'doorbell-test-0000000000000000',
                         payload, digest(payload)) == (202, 'queued')
    http = Mock()
    http.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 9}})
    assert VisitWorker(store, http).run_one()
    button = json.loads(http.post.call_args.kwargs['data']['reply_markup'])['inline_keyboard'][0][0]
    callback = make_callback(data=button['callback_data'])

    def queue(*args):
        return queue_telegram_flash(store, *args)

    wrong_message = {**callback, 'message': {**callback['message'], 'message_id': 99}}
    respond_to_callback(http, API_BASE, wrong_message, queue)
    assert http.post.call_args.kwargs['json']['show_alert'] is True
    assert claim_flash(store, 'doorbell-test') is None
    respond_to_callback(http, API_BASE, callback, queue)
    assert 'Đang chờ' in http.post.call_args.kwargs['json']['text']
    command = claim_flash(store, 'doorbell-test')
    assert command and acknowledge_flash(store, 'doorbell-test', command)
    respond_to_callback(http, API_BASE, callback, queue)
    assert 'đã xác nhận' in http.post.call_args.kwargs['json']['text']
    assert claim_flash(store, 'doorbell-test') is None


def test_poller_backs_off_without_removing_webhook_and_advances_offset():
    stop = Mock()
    stop.is_set.return_value = False
    poll_requests = []

    def post(url, *, json, **kwargs):
        if url.endswith('/answerCallbackQuery'):
            return SimpleNamespace(status_code=200)
        assert url.endswith('/getUpdates')
        poll_requests.append(json)
        if len(poll_requests) == 1:
            return SimpleNamespace(status_code=409)
        if len(poll_requests) == 2:
            return SimpleNamespace(status_code=200, json=lambda: {
                'ok': True, 'result': [{'update_id': 41, 'callback_query': make_callback()}],
            })
        stop.is_set.return_value = True
        return SimpleNamespace(status_code=200, json=lambda: {'ok': True, 'result': []})

    queue = Mock(return_value='queued')
    poll_callbacks(SimpleNamespace(post=post), API_BASE, stop, queue)
    assert poll_requests[0]['allowed_updates'] == ['callback_query']
    assert 'offset' not in poll_requests[1]
    assert poll_requests[2]['offset'] == 42
    stop.wait.assert_called_once_with(30)
    queue.assert_called_once_with(11, '12345', 9)


def test_rental_lookup_answers_one_private_authorized_message(tmp_path, monkeypatch):
    store = RentalStore(tmp_path / 'telegram.sqlite')
    store.initialize()
    with store.write() as db:
        db.execute("INSERT INTO houses(id,name,telegram_chat_id) VALUES(1,'Dãy A','12345'),(2,'Dãy B','23456')")
    reply = Mock(return_value='Dãy A còn 2 phòng trống.')
    monkeypatch.setattr(telegram_controls, 'answer_telegram_lookup', reply)
    monkeypatch.setattr(telegram_controls, 'Thread',
                        lambda target, args, **kwargs: SimpleNamespace(start=lambda: target(*args)))
    outgoing = MagicMock()
    outgoing.__enter__.return_value = outgoing
    outgoing.post.return_value.status_code = 200
    monkeypatch.setattr(telegram_controls.requests, 'Session', lambda: outgoing)
    stop = Mock()
    stop.is_set.return_value = False
    menu = DoorMenu(store, API_BASE, stop, None, 1)
    question = 'Dãy A còn phòng nào trống?'
    message = {'message_id': 7, 'text': question, 'date': int(time()),
               'chat': {'type': 'private', 'id': 12345}, 'from': {'id': 12345}}

    menu.message(Mock(), {**message, 'chat': {'type': 'group', 'id': 12345}})
    menu.message(Mock(), {**message, 'chat': {'type': 'private', 'id': 23456}, 'from': {'id': 23456}})
    reply.assert_not_called()
    menu.message(Mock(), message)
    menu.message(Mock(), message)
    reply.assert_called_once_with(store, '12345', question, house_id=1)
    outgoing.post.assert_called_once()
    payload = outgoing.post.call_args.kwargs['json']
    assert payload['chat_id'] == '12345'
    assert payload['text'] == 'Dãy A còn 2 phòng trống.'
    assert payload['reply_parameters']['message_id'] == 7
    assert payload['protect_content'] is True


def test_private_door_menu_and_standalone_flash(tmp_path, monkeypatch):
    store = RentalStore(tmp_path / 'door-menu.sqlite')
    store.initialize()
    initialize_flash(store)
    with store.connect() as db:
        db.execute("INSERT INTO houses(id,name,telegram_chat_id) VALUES(1,'Dãy A','12345')")
    store.register_device(1, 'doorbell-01')
    http = Mock()
    http.post.return_value.status_code = 200
    stop = Mock()
    previews = Mock()
    previews.snapshot.return_value = (b'fresh-jpeg', 200)
    menu = DoorMenu(store, API_BASE, stop, previews, 1)
    message = {'chat': {'type': 'private', 'id': 12345}, 'from': {'id': 12345},
               'date': int(time()), 'text': '/cuachung'}
    menu.message(http, {**message, 'chat': {'type': 'supergroup', 'id': 12345}})
    menu.message(http, {**message, 'from': {'id': 12346}})
    assert not http.post.called
    menu.message(http, message)
    rows = http.post.call_args.kwargs['json']['reply_markup']['inline_keyboard']
    assert [row[0]['callback_data'] for row in rows] == ['door:photo:doorbell-01', 'door:flash:doorbell-01']

    jobs = []
    thread = lambda target, args, **kwargs: SimpleNamespace(start=lambda: jobs.append((target, args)))
    monkeypatch.setattr('rental.telegram_controls.Thread', thread)
    queue = Mock()
    denied = make_callback(chat_id=12346, sender_id=12346, data='door:photo:doorbell-01')
    respond_to_callback(http, API_BASE, denied, queue, menu)
    assert http.post.call_args.kwargs['json']['show_alert'] is True and not jobs
    photo = {**make_callback(data='door:photo:doorbell-01'), 'id': 'photo-1'}
    respond_to_callback(http, API_BASE, photo, queue, menu)
    respond_to_callback(http, API_BASE, photo, queue, menu)
    assert len(jobs) == 1
    _, args = jobs.pop()
    action, *supplied = args
    stop.is_set.return_value = False
    action(http, *supplied)
    assert http.post.call_args.kwargs['files']['photo'][1] == b'fresh-jpeg'
    menu.pending.release()

    button = make_callback(data='door:flash:doorbell-01')
    respond_to_callback(http, API_BASE, button, queue, menu)
    respond_to_callback(http, API_BASE, button, queue, menu)
    command = claim_flash(store, 'doorbell-01')
    assert command and acknowledge_flash(store, 'doorbell-01', command)
    assert len(jobs) == 1 and claim_flash(store, 'doorbell-01') is None
    assert queue_direct_flash(store, 'doorbell-01', chat_id='12346') == ('denied', None)
    queue.assert_not_called()
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM visits').fetchone()[0] == 0
