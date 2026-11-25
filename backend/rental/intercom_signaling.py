import base64
import hmac
import os
import re
from dataclasses import dataclass
from threading import Lock
from time import monotonic, time

from flask import abort, request


CLIENT_ID = re.compile(r'[0-9a-f]{32}')
PEER_TIMEOUT = 10
MAX_ROOMS = 64
MAX_SIGNAL_BYTES = 24 * 1024


def ice_servers(visit_id):
    servers = []
    stun = os.getenv('RENTAL_INTERCOM_STUN_URL', '').strip()
    if stun.startswith('stun:'):
        servers.append({'urls': stun})
    turn = [url.strip() for url in os.getenv('RENTAL_INTERCOM_TURN_URLS', '').split(',')
            if url.strip().startswith(('turn:', 'turns:'))]
    secret = os.getenv('RENTAL_INTERCOM_TURN_SECRET', '')
    if turn and len(secret) >= 32:
        username = f'{int(time()) + 600}:intercom-{visit_id}'
        digest = hmac.digest(secret.encode(), username.encode(), 'sha1')
        servers.append({'urls': turn, 'username': username, 'credential': base64.b64encode(digest).decode()})
    return servers


@dataclass
class Participant:
    client: str
    seen: float
    target: str = ''
    description: dict | None = None


class Signaling:
    def __init__(self):
        self.lock = Lock()
        self.calls = {}

    def remove(self, visits):
        with self.lock:
            for visit in visits:
                self.calls.pop(visit, None)

    def exchange(self, visit_id, side):
        if not request.is_json:
            abort(415)
        request.max_content_length = MAX_SIGNAL_BYTES
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        client = body.get('client')
        if not isinstance(client, str) or not CLIENT_ID.fullmatch(client):
            abort(400)
        target = body.get('target', '')
        description = body.get('description')
        other = 'operator' if side == 'station' else 'station'
        if description is not None:
            expected = 'offer' if side == 'operator' else 'answer'
            sdp = description.get('sdp') if isinstance(description, dict) else None
            if (not isinstance(target, str) or not CLIENT_ID.fullmatch(target) or not isinstance(sdp, str)
                    or not sdp.startswith('v=0') or len(sdp) > 16000 or description.get('type') != expected):
                abort(400)
            description = {'type': expected, 'sdp': sdp}
        now = monotonic()
        with self.lock:
            for key, room in list(self.calls.items()):
                for who, participant in list(room.items()):
                    if now - participant.seen >= PEER_TIMEOUT:
                        del room[who]
                if not room:
                    del self.calls[key]
            room = self.calls.get(visit_id)
            participant = room.get(side) if room else None
            if body.get('leave') is True:
                if participant and participant.client == client:
                    del room[side]
                    if not room:
                        del self.calls[visit_id]
                return '', 204
            if participant and participant.client != client:
                return {'error': 'peer_in_use'}, 409
            if room is None:
                if len(self.calls) >= MAX_ROOMS:
                    abort(429)
                room = self.calls[visit_id] = {}
            if participant is None:
                participant = room[side] = Participant(client, now)
            participant.seen = now
            peer = room.get(other)
            if description is not None and peer and target == peer.client:
                participant.target = target
                participant.description = description
            return {'peer': peer.client if peer else None,
                    'description': peer.description if peer and peer.target == client else None}
