import json
import logging
import re
import sqlite3
from collections import deque
from threading import BoundedSemaphore, Thread
from time import monotonic, time

import requests

from rental.flash import direct_flash_status, queue_direct_flash
from rental.hermes_lookup import QUESTION_LIMIT, answer_telegram_lookup, telegram_lookup_allowed
from rental.store import digest, format_time


logger = logging.getLogger(__name__)
POLL_SECONDS = 15
FLASH_CALLBACK = re.compile(r'flash:([1-9][0-9]{0,18})')
DOOR_CALLBACK = re.compile(r'door:(photo|flash):([A-Za-z0-9_-]{1,48})')
DOOR_MENU = re.compile(r'/cuachung(?:@[A-Za-z0-9_]+)?')
LOOKUP_COMMAND = re.compile(r'/hoi(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?', re.DOTALL)
LOOKUP_HELP = 'Bạn có thể hỏi “Dãy A còn phòng nào trống?” hoặc “Phòng 02 tháng này còn bao nhiêu tiền?”.'
REPLIES = {
    'queued': 'Đã gửi yêu cầu. Đang chờ ESP32 xác nhận.',
    'existing': 'Lượt chuông này đã có yêu cầu. Xem trạng thái trên web.',
    'confirmed': 'ESP32 đã xác nhận bật đèn mô phỏng trong 3 giây.',
    'unavailable': 'Thiết bị chưa sẵn sàng hoặc lượt chuông đã hết hạn.',
    'denied': 'Tin nhắn này không có quyền mô phỏng mở cửa.',
}


def positive_id(value):
    return type(value) is int and 0 < value < 2**63


def private_recipient(message, sender):
    if not isinstance(message, dict) or not isinstance(sender, dict) or sender.get('is_bot'):
        return None
    chat = message.get('chat')
    if not isinstance(chat, dict) or chat.get('type') != 'private':
        return None
    chat_id = chat.get('id')
    sender_id = sender.get('id')
    return str(chat_id) if positive_id(chat_id) and positive_id(sender_id) and sender_id == chat_id else None


def flash_keyboard(recipient_chat_id, visit_id):
    # Private Telegram recipients have positive IDs. Groups still get the photo.
    if (isinstance(recipient_chat_id, str) and re.fullmatch(r'[1-9][0-9]{0,15}', recipient_chat_id)
            and positive_id(visit_id)):
        return json.dumps({'inline_keyboard': [[{
            'text': 'Mở cửa mô phỏng',
            'callback_data': f'flash:{visit_id}',
        }]]}, ensure_ascii=False)
    return None


def parse_flash_callback(callback):
    data = callback.get('data')
    token = FLASH_CALLBACK.fullmatch(data) if isinstance(data, str) else None
    message = callback.get('message')
    sender = callback.get('from')
    if token is None or not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    chat = message.get('chat')
    if not isinstance(chat, dict) or chat.get('type') != 'private' or sender.get('is_bot'):
        return None
    visit_id, chat_id, message_id = int(token[1]), chat.get('id'), message.get('message_id')
    if not all(map(positive_id, (visit_id, chat_id, message_id, sender.get('id')))) or sender['id'] != chat_id:
        return None
    return visit_id, str(chat_id), message_id


def respond_to_callback(http, api_base, callback, queue, menu=None):
    if not isinstance(callback, dict):
        return
    callback_id = callback.get('id')
    if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 128:
        return
    direct = DOOR_CALLBACK.fullmatch(callback['data']) if isinstance(callback.get('data'), str) else None
    if menu and direct:
        answer, alert = menu.callback(callback, *direct.groups())
    else:
        command = parse_flash_callback(callback)
        outcome = queue(*command) if command is not None else 'denied'
        answer, alert = REPLIES.get(outcome, REPLIES['denied']), outcome in {'denied', 'unavailable'}
    try:
        http.post(f'{api_base}/answerCallbackQuery', timeout=(4, 4), allow_redirects=False,
                  json={'callback_query_id': callback_id, 'text': answer, 'show_alert': alert, 'cache_time': 0})
    except requests.RequestException:
        logger.warning('Telegram callback answer could not be sent.')


class DoorMenu:
    def __init__(self, store, api, stop, previews, house_id):
        self.store, self.api, self.stop, self.previews, self.house_id = store, api, stop, previews, house_id
        self.pending = BoundedSemaphore(2)
        self.seen_photos = deque(maxlen=128)
        self.lookup_pending = BoundedSemaphore(2)
        self.seen_lookups = deque(maxlen=128)

    def targets(self, chat_id, device=None):
        with self.store.connect() as db:
            return db.execute('''SELECT d.id, h.name FROM devices d JOIN houses h ON d.house_id=h.id
                                 WHERE h.telegram_chat_id=? AND (? IS NULL OR d.house_id=?)
                                   AND (? IS NULL OR d.id=?) ORDER BY h.name, d.id''',
                              (chat_id, self.house_id, self.house_id, device, device)).fetchall()

    def send(self, http, method, **payload):
        try:
            result = http.post(f'{self.api}/{method}', timeout=(4, 10), allow_redirects=False, **payload)
            if result.status_code != 200:
                logger.warning('Telegram door controls %s returned HTTP %d.', method, result.status_code)
        except requests.RequestException:
            logger.warning('Telegram door controls %s temporarily unavailable.', method)

    def message(self, http, message):
        if not isinstance(message, dict):
            return
        text = message.get('text')
        date = message.get('date')
        if not isinstance(text, str) or not positive_id(date) or not 0 <= time() - date < 180:
            return
        chat = private_recipient(message, message.get('from'))
        if not chat:
            return
        text = text.strip()
        if not DOOR_MENU.fullmatch(text):
            self.lookup_message(http, chat, message.get('message_id'), text)
            return
        targets = self.targets(chat)
        if not targets:
            return
        buttons = []
        for target in targets[:12]:
            suffix = f" · {target['name'][:22]} ({target['id'][:12]})" if len(targets) > 1 else ''
            buttons.extend([[{'text': label + suffix, 'callback_data': f"door:{kind}:{target['id']}"}]
                            for kind, label in [('photo', 'Chụp ảnh ngay'), ('flash', 'Mở cửa mô phỏng')]])
        self.send(http, 'sendMessage', json={'chat_id': chat, 'text': 'Điều khiển cửa chung',
                                            'reply_markup': {'inline_keyboard': buttons}})

    def lookup_message(self, http, chat, message_id, text):
        command = LOOKUP_COMMAND.fullmatch(text)
        if not text or (text.startswith('/') and not command) or not positive_id(message_id):
            return
        key = (chat, message_id)
        if key in self.seen_lookups or not telegram_lookup_allowed(self.store, chat, house_id=self.house_id):
            return
        self.seen_lookups.append(key)
        question = (command[1] or '').strip() if command else text
        if not question or len(question) > QUESTION_LIMIT:
            self.send_lookup_reply(http, chat, message_id, LOOKUP_HELP)
            return
        if not self.lookup_pending.acquire(blocking=False):
            self.send_lookup_reply(http, chat, message_id, 'Hermes đang tra cứu. Bạn thử lại sau vài giây nhé.')
            return
        try:
            Thread(target=self.lookup_reply, args=(chat, message_id, question), daemon=True,
                   name='telegram-rental-lookup').start()
        except Exception:
            self.lookup_pending.release()
            raise

    def send_lookup_reply(self, http, chat, message_id, text):
        self.send(http, 'sendMessage', json={
            'chat_id': chat, 'text': text[:3900], 'protect_content': True,
            'reply_parameters': {'message_id': message_id, 'allow_sending_without_reply': True},
        })

    def lookup_reply(self, chat, message_id, question):
        try:
            reply = answer_telegram_lookup(self.store, chat, question, house_id=self.house_id)
            if reply and not self.stop.is_set() and telegram_lookup_allowed(self.store, chat, house_id=self.house_id):
                with requests.Session() as http:
                    self.send_lookup_reply(http, chat, message_id, reply)
        except (requests.RequestException, sqlite3.OperationalError):
            logger.warning('Telegram rental lookup temporarily unavailable.')
        finally:
            self.lookup_pending.release()

    def callback(self, callback, action, device):
        chat = private_recipient(callback.get('message'), callback.get('from'))
        rows = self.targets(chat, device) if chat else []
        if not rows:
            return 'Tài khoản Telegram này chưa được cấu hình để điều khiển cửa.', True
        if action == 'photo':
            if self.previews is None:
                return 'Chức năng chụp ảnh chưa có kết nối với web.', True
            if callback['id'] in self.seen_photos:
                return 'Đã nhận yêu cầu chụp ảnh.', False
        if not self.pending.acquire(blocking=False):
            return 'Đang xử lý yêu cầu trước. Thử lại sau vài giây.', True
        try:
            if action == 'photo':
                self.seen_photos.append(callback['id'])
                args = self.photo, chat, device, rows[0]['name']
                answer = 'Đang chờ ảnh mới từ ESP32.'
            else:
                outcome, command = queue_direct_flash(
                    self.store, device, chat_id=chat, house_id=self.house_id,
                    request_key=digest(callback['id'].encode()),
                )
                if outcome != 'queued':
                    self.pending.release()
                    return {
                        'confirmed': REPLIES['confirmed'],
                        'existing': 'Yêu cầu trước đang được xử lý hoặc vừa hoàn tất.',
                        'expired': 'Yêu cầu cũ đã hết hạn. Bấm lại để gửi lệnh mới.',
                    }.get(outcome, REPLIES['denied']), outcome in {'denied', 'expired'}
                args = self.flash_confirmation, chat, device, command
                answer = 'Đã gửi lệnh. Đang chờ ESP32 xác nhận.'
            Thread(target=self.complete, args=args, daemon=True, name='telegram-door-action').start()
        except Exception:
            self.pending.release()
            raise
        return answer, False

    def complete(self, action, *args):
        try:
            with requests.Session() as http:
                action(http, *args)
        except sqlite3.OperationalError:
            logger.warning('Telegram door action interrupted by storage.')
        finally:
            self.pending.release()

    def photo(self, http, chat, device, name):
        frame, status = self.previews.snapshot(device)
        if self.stop.is_set() or not self.targets(chat, device):
            return
        if frame is None:
            text = ('Camera đang xử lý yêu cầu khác.' if status == 429 else
                    'Chưa nhận được ảnh mới. Hãy kiểm tra nguồn và Wi-Fi của ESP32.')
            self.send(http, 'sendMessage', json={'chat_id': chat, 'text': text})
            return
        self.send(http, 'sendPhoto',
                  data={'chat_id': chat, 'caption': f'{name} · Cửa chung\nẢnh chụp ngay · {format_time(int(time()))}'},
                  files={'photo': ('cua-chung.jpg', frame, 'image/jpeg')})

    def flash_confirmation(self, http, chat, device, command):
        deadline = monotonic() + 46
        status = None
        while monotonic() < deadline and not self.stop.is_set():
            status = direct_flash_status(self.store, device, command)
            if status not in {'queued', 'sent'}:
                break
            self.stop.wait(1)
        if not self.stop.is_set() and self.targets(chat, device):
            text = REPLIES['confirmed'] if status == 'confirmed' else 'ESP32 chưa xác nhận bật đèn. Kiểm tra nguồn và Wi-Fi.'
            self.send(http, 'sendMessage', json={'chat_id': chat, 'text': text})


def poll_callbacks(http, api_base, stop, queue, menu=None):
    offset = None
    last_error = None
    while not stop.is_set():
        params = {'timeout': POLL_SECONDS, 'allowed_updates': ['callback_query', 'message'] if menu else ['callback_query']}
        if offset is not None:
            params['offset'] = offset
        try:
            response = http.post(f'{api_base}/getUpdates', json=params, timeout=(4, POLL_SECONDS + 5),
                                 allow_redirects=False)
            if response.status_code != 200:
                status = response.status_code
                if last_error != status:
                    if status == 409:
                        logger.warning('Telegram callback polling conflicts with another consumer or webhook.')
                    else:
                        logger.warning('Telegram callback polling returned HTTP %d.', status)
                last_error = status
                stop.wait(30 if status == 409 else 5)
                continue
            payload = response.json()
            updates = payload.get('result') if isinstance(payload, dict) and payload.get('ok') is True else None
            if not isinstance(updates, list):
                raise ValueError('Invalid Telegram update list')
            if not updates:
                offset = None  # The previous offset was confirmed once Telegram accepted it.
            for update in updates:
                if stop.is_set():
                    return
                if isinstance(update, dict) and positive_id(update.get('update_id')):
                    respond_to_callback(http, api_base, update.get('callback_query'), queue, menu)
                    if menu:
                        menu.message(http, update.get('message'))
                    offset = update['update_id'] + 1
            last_error = None
        except (requests.RequestException, ValueError, sqlite3.OperationalError):
            if last_error != 'temporary':
                logger.warning('Telegram callback polling temporarily unavailable.')
            last_error = 'temporary'
            stop.wait(5)


def start_telegram_controls(store, bot_token, stop, previews=None, house_id=None):
    if not bot_token:
        return

    def run():
        from rental.flash import initialize_flash, queue_telegram_flash

        while not stop.is_set():
            try:
                initialize_flash(store)
            except sqlite3.OperationalError:
                logger.warning('Telegram controls waiting for rental storage.')
                stop.wait(5)
            else:
                break
        with requests.Session() as http:
            api_base = f'https://api.telegram.org/bot{bot_token}'
            poll_callbacks(http, api_base, stop, lambda *command: queue_telegram_flash(store, *command),
                           DoorMenu(store, api_base, stop, previews, house_id))

    Thread(target=run, name='rental-telegram-controls', daemon=True).start()
