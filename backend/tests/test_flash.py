import pytest

from rental import app, flash
from rental.store import RentalStore
from test_accounts import issue, post, sign_in, USER


@pytest.fixture
def doorbell(tmp_path, monkeypatch):
    now = [1_000_000]
    monkeypatch.setattr(app, 'time', lambda: now[0])
    monkeypatch.setattr(flash, 'time', lambda: now[0])
    store = RentalStore(tmp_path / 'flash.sqlite')
    store.initialize()
    with store.connect() as db:
        db.executemany('INSERT INTO houses(id,name,telegram_chat_id) VALUES(?,?,?)',
                       [(1, 'Dãy A', '321'), (2, 'Dãy B', '456')])
    devices = {}
    for house_id in (1, 2):
        name = f'doorbell-0{house_id}'
        token = store.register_device(house_id, name)
        devices[house_id] = {'Authorization': f'Bearer {token}', 'X-Device-ID': name,
                             'X-Doorbell-Flash-Capable': '1'}

    def visit(visit_id, house_id=1):
        event_id = f'doorbell-0{house_id}-{visit_id:016x}'
        chat_id = '321' if house_id == 1 else '456'
        with store.write() as db:
            db.execute('''INSERT INTO visits(id,device_id,house_id,event_id,recipient_chat_id,jpeg,image_hash,
                                             received_at,notification_status,telegram_message_id)
                          VALUES(?,?,?,?,?,X'00',' test hash ',?,'sent',?)''',
                       (visit_id, f'doorbell-0{house_id}', house_id, event_id, chat_id, now[0], 50 + visit_id))

    visit(1)
    application = app.create_app(store, 'temporary owner test password')
    owner = application.test_client()
    assert sign_in(owner).status_code == 303
    return store, owner, application.test_client(), devices, now, visit


def test_flash_web_and_telegram_share_one_command_with_device_confirmation(doorbell):
    store, owner, device, headers, _, _ = doorbell
    dashboard = owner.get('/').get_data(as_text=True)
    assert 'Mở cửa mô phỏng' in dashboard
    assert owner.post('/visits/1/flash').status_code == 403
    assert post(owner, '/visits/1/flash', {}).status_code == 303
    assert owner.get('/visits/1/flash').json == {'status': 'queued'}
    assert flash.queue_telegram_flash(store, 1, '321', 51) == 'existing'
    demand = '/api/device/live/demand'
    assert device.get(demand).status_code == 401
    assert 'X-Doorbell-Flash' not in device.get(demand, headers=headers[2]).headers
    assert 'X-Doorbell-Flash' not in device.head(demand, headers=headers[1]).headers
    old_header = {k: v for k, v in headers[1].items() if k != 'X-Doorbell-Flash-Capable'}
    assert 'X-Doorbell-Flash' not in device.get(demand, headers=old_header).headers
    result = device.get(demand, headers=headers[1])
    assert result.status_code == 204 and result.headers['Cache-Control'] == 'no-store'
    command_id = result.headers['X-Doorbell-Flash']
    assert flash.COMMAND_ID.fullmatch(command_id)
    assert 'X-Doorbell-Flash' not in device.get(demand, headers=headers[1]).headers
    assert owner.get('/visits/1/flash').json == {'status': 'sent'}
    ack = f'/api/device/flash/{command_id}/ack'
    assert device.post(ack, headers=headers[2]).status_code == 404
    assert device.post(ack, headers=headers[1]).status_code == 204
    assert device.post(ack, headers=headers[1]).status_code == 204
    assert owner.get('/visits/1/flash').json == {'status': 'confirmed'}
    assert flash.queue_telegram_flash(store, 1, '321', 51) == 'confirmed'


def test_telegram_binding_expiry_and_latest_visit(doorbell):
    store, owner, device, headers, now, visit = doorbell
    for chat_id, message_id in [('987', 51), ('-321', 51), ('321', 52)]:
        assert flash.queue_telegram_flash(store, 1, chat_id, message_id) == 'denied'
    assert flash.queue_telegram_flash(store, 1, '321', 51) == 'queued'
    visit(2)
    assert 'X-Doorbell-Flash' not in device.get('/api/device/live/demand', headers=headers[1]).headers
    assert owner.get('/visits/1/flash').json == {'status': 'expired'}
    assert flash.queue_telegram_flash(store, 2, '321', 52) == 'queued'
    now[0] += flash.QUEUE_SECONDS
    assert 'X-Doorbell-Flash' not in device.get('/api/device/live/demand', headers=headers[1]).headers
    assert owner.get('/visits/2/flash').json == {'status': 'expired'}
    visit(3)
    assert flash.queue_telegram_flash(store, 3, '321', 53) == 'queued'
    with store.write() as db:
        db.execute("UPDATE houses SET telegram_chat_id='987' WHERE id=1")
    assert 'X-Doorbell-Flash' not in device.get('/api/device/live/demand', headers=headers[1]).headers


def test_direct_flash_needs_no_visit_and_ack_is_scoped(doorbell):
    store, owner, device, headers, now, _ = doorbell
    with store.write() as db:
        db.execute('DELETE FROM visits')
    endpoint = '/camera/doorbell-01/flash'
    assert owner.post(endpoint).status_code == 403
    command = post(owner, endpoint, {})
    assert command.status_code == 202
    status = command.json['status_url']
    assert owner.get(status).json == {'status': 'queued'}
    replay = post(owner, endpoint, {}).json
    assert replay == {'status': 'existing', 'status_url': status}
    assert 'X-Doorbell-Flash' not in device.get('/api/device/live/demand', headers=headers[2]).headers
    claim = device.get('/api/device/live/demand', headers=headers[1])
    assert claim.status_code == 204
    command_id = claim.headers['X-Doorbell-Flash']
    assert owner.get(status).json == {'status': 'sent'}
    assert owner.get(f'/camera/doorbell-02/flash/{command_id}').status_code == 404
    assert device.post(f'/api/device/flash/{command_id}/ack', headers=headers[2]).status_code == 404
    assert device.post(f'/api/device/flash/{command_id}/ack', headers=headers[1]).status_code == 204
    assert owner.get(status).json == {'status': 'confirmed'}
    assert 'X-Doorbell-Flash' not in device.get('/api/device/live/demand', headers=headers[1]).headers
    now[0] += flash.DIRECT_COOLDOWN
    assert post(owner, endpoint, {}).json['status_url'] != status


def test_manager_scope_and_flash_with_active_camera(doorbell):
    _, owner, device, headers, now, visit = doorbell
    visit(2, house_id=2)
    issue(owner, 'managed-a', 'manager', house_id=1)
    manager = owner.application.test_client()
    assert sign_in(manager, 'managed-a', USER).status_code == 303
    assert manager.get('/visits/2/flash').status_code == 404
    assert post(manager, '/visits/2/flash', {}).status_code == 404
    assert post(manager, '/camera/doorbell-01/start', {}).status_code == 200
    assert post(manager, '/visits/1/flash', {}).status_code == 303
    result = device.get('/api/device/live/demand', headers=headers[1])
    assert result.status_code == 200 and len(result.get_data(as_text=True)) == 32
    command_id = result.headers['X-Doorbell-Flash']
    now[0] += flash.ACK_SECONDS
    assert device.post(f'/api/device/flash/{command_id}/ack', headers=headers[1]).status_code == 404
    assert manager.get('/visits/1/flash').json == {'status': 'expired'}
