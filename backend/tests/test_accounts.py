import io
import re
from unicodedata import normalize

import pytest
from PIL import Image

from rental.app import create_app
from rental.store import RentalStore, digest


OWNER = 'temporary owner test password'
USER = 'temporary user test password'
DEVICE = 'valid-test-device-key' * 2


def post(client, url, data):
    with client.session_transaction() as state:
        csrf = state['csrf']
    return client.post(url, data={'csrf': csrf, **data})


def sign_in(client, username='chu-tro', password=OWNER):
    assert client.get('/login').status_code == 200
    return post(client, '/login', {'username': username, 'password': password})


@pytest.fixture
def app(tmp_path):
    store = RentalStore(tmp_path / 'historical.db')
    store.initialize()
    with store.write() as db:
        db.executemany('INSERT INTO houses(id,name) VALUES(?,?)', [(1,'Dãy thử A'),(2,'Dãy riêng B')])
        db.executemany('INSERT INTO rooms(id,house_id,code) VALUES(?,?,?)', [(1,1,'A01'),(2,2,'B01')])
        db.executemany('INSERT INTO stays(id,room_id,name) VALUES(?,?,?)', [(1,1,'Người thử A'),(2,2,'Người riêng B')])
        db.execute("INSERT INTO devices VALUES('remote-b',2,?)", (digest(DEVICE.encode()),))
    return create_app(store, OWNER)


def issue(owner, username, role, **scope):
    result = post(owner, '/accounts', {'username':username,'display_name':username,'password':USER,'role':role,**scope})
    assert result.status_code == 303


def test_same_site_manager_can_manage_but_never_reads_or_edits_other_site(app):
    owner = app.test_client()
    assert sign_in(owner).status_code == 303
    issue(owner,'manager-a','manager',house_id=1)
    manager = app.test_client()
    assert sign_in(manager,'manager-a',USER).location.endswith('/')
    page = manager.get('/').get_data(as_text=True)
    assert 'Người thử A' in page and 'Người riêng B' not in page
    assert manager.get('/?house=2').status_code == 404
    assert post(manager,'/manage/room',{'house_id':2,'code':'B07'}).status_code == 404
    assert post(manager,'/manage/room',{'house_id':1,'code':'A07'}).status_code == 303
    assert post(manager,'/manage/checkout',{'stay_id':2}).status_code == 404
    assert manager.get('/stays/2/face-samples').status_code == 404
    assert manager.get('/stays/2/checkout').status_code == 404
    assert post(manager,'/manage/telegram',{'house_id':1,'chat_id':'123'}).status_code == 403
    assert post(manager,'/manage/device',{'house_id':1,'device_id':'another'}).status_code == 403
    assert post(manager,'/camera/remote-b/start',{}).status_code == 404
    assert manager.get('/accounts').status_code == 403
    photo = io.BytesIO()
    Image.new('RGB',(40,40),'white').save(photo,'JPEG')
    assert app.test_client().post('/api/device/doorbell', data=photo.getvalue(), content_type='image/jpeg',
                                 headers={'Authorization': f'Bearer {DEVICE}', 'X-Device-ID':'remote-b',
                                          'X-Event-ID':'remote-b-'+'0'*16}).status_code == 202
    assert manager.get('/visits/1/photo').status_code == 404
    assert owner.get('/visits/1/photo').status_code == 200


def test_tenant_and_technician_cannot_use_camera_and_account_revocation_takes_effect(app):
    owner = app.test_client()
    sign_in(owner)
    issue(owner,'resident-a','tenant',stay_id=1)
    issue(owner,'service-a','technician',house_id=1)
    resident = app.test_client()
    technician = app.test_client()
    assert sign_in(resident,'resident-a',USER).location.endswith('/operations')
    assert sign_in(technician,'service-a',USER).location.endswith('/operations')
    for client in (resident,technician):
        assert client.get('/').location.endswith('/operations')
        assert client.get('/operations').status_code == 200
        assert client.get('/camera').status_code == 403
        assert client.get('/visits/1/photo').status_code == 403
        assert client.get('/stays/1/checkout').status_code == 403
        assert post(client,'/manage/checkout',{'stay_id':1}).status_code == 403
    assert post(owner,'/accounts/1/disable',{}).status_code == 303
    assert resident.get('/operations').location.endswith('/login')
    assert sign_in(resident,'resident-a',USER).status_code == 401


def test_legacy_admin_flag_does_not_grant_access(app):
    client = app.test_client()
    with client.session_transaction() as state:
        state['admin'] = True
    assert client.get('/accounts').location.endswith('/login')
    assert client.get('/camera').location.endswith('/login')


def test_owner_can_use_existing_long_password(tmp_path):
    store = RentalStore(tmp_path / 'existing.db')
    store.initialize()
    password = 'long test password ' * 9
    client = create_app(store, password).test_client()
    assert sign_in(client, '', password).status_code == 303
    assert client.get('/accounts').status_code == 200


def test_reassignment_requires_new_password_and_revokes_old_sessions(app):
    owner = app.test_client()
    sign_in(owner)
    issue(owner,'manager-a','manager',house_id=1)
    employee = app.test_client()
    assert sign_in(employee,'manager-a',USER).status_code == 303
    response = post(owner,'/accounts/1/scope',{'display_name':'Manager B','role':'manager','house_id':2,'password':USER})
    assert response.status_code == 303
    assert 'mật khẩu mới phải khác' in owner.get('/accounts').get_data(as_text=True)
    assert employee.get('/?house=2').status_code == 404
    replacement = 'fresh password for reassigned employee'
    assert post(owner,'/accounts/1/scope',{'display_name':'Manager B','role':'manager','house_id':2,
                                          'password':replacement}).status_code == 303
    assert employee.get('/camera').location.endswith('/login')
    assert sign_in(employee,'manager-a',USER).status_code == 401
    assert sign_in(employee,'manager-a',replacement).status_code == 303
    page = employee.get('/').get_data(as_text=True)
    assert 'Người riêng B' in page and 'Người thử A' not in page
    assert post(owner,'/accounts/1/disable',{}).status_code == 303
    assert employee.get('/').location.endswith('/login')
    assert post(owner,'/accounts/1/enable',{'password':replacement}).status_code == 303
    assert sign_in(employee,'manager-a',replacement).status_code == 401
    assert post(owner,'/accounts/1/enable',{'password':USER}).status_code == 303
    assert sign_in(employee,'manager-a',USER).status_code == 303


def test_invitation_registers_the_correct_resident_and_cannot_be_reused(tmp_path):
    store = RentalStore(tmp_path / 'invites.db')
    store.initialize()
    app = create_app(store, OWNER, single_house=True)
    owner = app.test_client()
    sign_in(owner)
    assert post(owner, '/manage/room', {'code': 'A01'}).status_code == 303
    assert post(owner, '/manage/stay', {'room_id': 1, 'name': 'Người thuê A'}).status_code == 303
    assert post(owner, '/manage/stay', {'room_id': 1, 'name': 'Người thuê khác'}).status_code == 303

    result = post(owner, '/accounts/invite', {'stay_id': 1})
    assert result.status_code == 201 and result.headers['Cache-Control'] == 'no-store'
    code = re.search(r'<input value="([A-Za-z0-9_-]{24})" readonly', result.text)[1]
    visitor = app.test_client()
    assert visitor.get('/register').status_code == 200
    form = {'code': code, 'username': 'resident-a', 'password': USER, 'confirmation': USER}
    assert visitor.post('/register', data=form).status_code == 403
    assert 'chưa khớp' in post(visitor, '/register', {**form, 'confirmation': OWNER}).text
    assert post(visitor, '/register', {**form, 'role': 'manager', 'stay_id': 2}).status_code == 303
    assert 'Đã tạo tài khoản người thuê' in visitor.get('/login').text
    assert post(visitor, '/register', {**form, 'username': 'resident-b'}).status_code == 400
    assert sign_in(visitor, 'resident-a', USER).location.endswith('/operations')
    assert 'Người thuê A' in visitor.get('/operations').text
    assert 'Người thuê khác' not in visitor.get('/operations').text
    assert visitor.get('/camera').status_code == 403
    assert post(owner, '/accounts/1/disable', {}).status_code == 303
    assert visitor.get('/operations').location.endswith('/login')


def test_reissued_expired_and_checked_out_invites_are_rejected(tmp_path):
    store = RentalStore(tmp_path / 'expired.db')
    store.initialize()
    app = create_app(store, OWNER, single_house=True)
    with store.write() as db:
        db.execute("INSERT INTO rooms(house_id,code) VALUES(1,'A01')")
        db.execute("INSERT INTO stays(room_id,name) VALUES(1,'Người thuê A')")
    owner = app.test_client()
    sign_in(owner)
    visitor = app.test_client()
    visitor.get('/register')

    def issue_code():
        result = post(owner, '/accounts/invite', {'stay_id': 1})
        return re.search(r'<input value="([A-Za-z0-9_-]{24})" readonly', result.text)[1]

    def submit_code(code):
        return post(visitor, '/register', {'code': code, 'username': 'resident-a',
                                           'password': USER, 'confirmation': USER})

    previous, current = issue_code(), issue_code()
    assert submit_code(previous).status_code == 400
    with store.write() as db:
        db.execute('UPDATE rental_account_invites SET expires_at=0')
    assert submit_code(current).status_code == 400
    latest = issue_code()
    assert post(owner, '/manage/checkout', {'stay_id': 1}).status_code == 303
    assert submit_code(latest).status_code == 400


def test_single_house_mode_uses_existing_day_a_and_limits_legacy_data(tmp_path):
    store = RentalStore(tmp_path / 'multi-house.db')
    store.initialize()
    with store.write() as db:
        db.executemany('INSERT INTO houses(id,name) VALUES(?,?)', [(1,'Dãy B'), (2,'Dãy A')])
        db.execute("INSERT INTO rooms(id,house_id,code) VALUES(1,1,'B01')")
        db.execute("INSERT INTO stays(id,room_id,name) VALUES(1,1,'Hồ sơ dãy B')")
        db.execute("INSERT INTO devices VALUES('remote-b',1,?)", (digest(DEVICE.encode()),))
    legacy = create_app(store, OWNER).test_client()
    sign_in(legacy)
    issue(legacy, 'manager-b', 'manager', house_id=1)
    app = create_app(store, OWNER, single_house=True)
    owner = app.test_client()
    sign_in(owner)
    dashboard = owner.get('/').text
    assert 'Tổng quan Dãy A' in dashboard and 'Thêm dãy trọ' not in dashboard and 'Hồ sơ dãy B' not in dashboard
    assert 'remote-b' not in owner.get('/camera').text
    assert 'remote-b' not in owner.get('/intercom').text
    assert 'manager-b' not in owner.get('/accounts').text
    assert sign_in(app.test_client(), 'manager-b', USER).status_code == 401
    assert owner.get('/operations?house=1').status_code == 404
    assert post(owner, '/manage/house', {'name': 'Dãy C'}).status_code == 404
    assert post(owner, '/manage/room', {'house_id': 1, 'code': 'B02'}).status_code == 404
    assert post(owner, '/manage/room', {'code': 'A01'}).status_code == 303
    assert post(owner, '/operations/notices', {'title': 'Thông báo', 'body': 'Dãy A'}).status_code == 303
    issue(owner, 'manager-a', 'manager')
    assert sign_in(app.test_client(), 'manager-a', USER).status_code == 303
    with store.connect() as db:
        assert db.execute("SELECT house_id FROM rooms WHERE code='A01'").fetchone()[0] == 2
        assert db.execute("SELECT house_id FROM rental_notices").fetchone()[0] == 2
        assert db.execute("SELECT name FROM stays WHERE id=1").fetchone()[0] == 'Hồ sơ dãy B'


def test_checkout_review_and_duplicate_names_preserve_distinct_roommates(tmp_path):
    store = RentalStore(tmp_path / 'checkout.db')
    store.initialize()
    app = create_app(store, OWNER, single_house=True)
    owner = app.test_client()
    sign_in(owner)
    assert post(owner, '/manage/room', {'code': '01'}).status_code == 303
    assert post(owner, '/manage/stay', {'room_id': 1, 'name': 'Nguyễn Lan'}).status_code == 303
    assert post(owner, '/manage/stay', {'room_id': 1, 'name': 'Trần Minh'}).status_code == 303

    duplicate = normalize('NFD', 'NGUYỄN  LAN')
    assert post(owner, '/manage/stay', {'room_id': 1, 'name': duplicate}).status_code == 303
    assert 'Phòng này đã có người đang thuê mang tên này.' in owner.get('/').text
    assert post(owner, '/manage/rename-resident', {'stay_id': 2, 'name': duplicate}).status_code == 303
    assert 'Phòng này đã có người đang thuê mang tên này.' in owner.get('/').text

    issue(owner, 'resident-a', 'tenant', stay_id=1)
    preview = owner.get('/stays/1/checkout')
    assert preview.status_code == 200
    assert 'Xác nhận trả phòng' in preview.text and 'Nguyễn Lan' in preview.text and 'Phòng 01' in preview.text
    with store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT name,ended_at FROM stays ORDER BY id')] == [
            ('Nguyễn Lan', None), ('Trần Minh', None)
        ]

    assert post(owner, '/manage/checkout', {'stay_id': 1}).status_code == 303
    assert owner.get('/stays/1/checkout').status_code == 404
    assert 'Đã trả phòng' in owner.get('/accounts').text
    assert post(owner, '/manage/stay', {'room_id': 1, 'name': 'Nguyễn Lan'}).status_code == 303
    with store.connect() as db:
        assert [row['name'] for row in db.execute('SELECT name FROM stays WHERE ended_at IS NULL ORDER BY id')] == [
            'Trần Minh', 'Nguyễn Lan'
        ]


def test_owner_can_revoke_current_invite_without_revoking_a_replacement(app):
    owner = app.test_client()
    sign_in(owner)
    issue(owner, 'manager-a', 'manager', house_id=1)
    manager = app.test_client()
    sign_in(manager, 'manager-a', USER)

    def invite():
        result = post(owner, '/accounts/invite', {'stay_id': 1})
        assert result.status_code == 201
        code = re.search(r'<input value="([A-Za-z0-9_-]{24})" readonly', result.text)[1]
        page = owner.get('/accounts')
        assert 'Có mã mời' in page.text
        revision = re.search(r'name="revision" value="([a-f0-9]{16})"', page.text)[1]
        return code, revision

    _, previous_revision = invite()
    code, revision = invite()
    revoke_path = '/accounts/invite/1/revoke'
    assert post(manager, revoke_path, {'revision': revision}).status_code == 403
    assert post(owner, revoke_path, {'revision': previous_revision}).status_code == 303
    assert 'Có mã mời' in owner.get('/accounts').text
    assert post(owner, revoke_path, {'revision': revision}).status_code == 303
    assert 'Đã hủy mã mời.' in owner.get('/accounts').text

    visitor = app.test_client()
    visitor.get('/register')
    assert post(visitor, '/register', {'code': code, 'username': 'tenant-a',
                                       'password': USER, 'confirmation': USER}).status_code == 400
