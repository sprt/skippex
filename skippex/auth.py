from time import sleep
from typing import Any, Dict, NamedTuple, Optional, Tuple
from urllib.parse import urlencode

import requests


class PlexApplication(NamedTuple):
    name: str
    identifier: str


class PlexAuthClient:
    # Reference: https://forums.plex.tv/t/authenticating-with-plex/609370

    _BASE_API_URL = 'https://plex.tv/api/v2'
    _BASE_AUTH_URL = 'https://app.plex.tv/auth'

    def __init__(self, app: PlexApplication):
        self._app = app

    def _make_request(self, method: str, endpoint: str, **kwargs: Dict[str, Any]) -> requests.Response:
        url = self._BASE_API_URL + endpoint
        return requests.request(method, url, **kwargs)

    def is_token_valid(self, token: str) -> bool:
        headers = {'Accept': 'application/json'}
        data = {
            'X-Plex-Product': self._app.name,
            'X-Plex-Client-Identifier': self._app.identifier,
            'X-Plex-Token': token,
        }
        r = self._make_request('GET', '/user', headers=headers, data=data)

        try:
            r.raise_for_status()
        except requests.HTTPError:
            if r.status_code == 401:
                return False
            raise
        else:
            return True

    def generate_pin(self) -> Tuple[int, str]:
        headers = {'Accept': 'application/json'}
        data = {
            'strong': 'true',
            'X-Plex-Product': self._app.name,
            'X-Plex-Client-Identifier': self._app.identifier,
        }
        r = self._make_request('POST', '/pins', headers=headers, data=data)
        r.raise_for_status()
        info = r.json()
        return info['id'], info['code']

    def generate_auth_url(self, pin_code: str) -> str:
        qs = urlencode({
            'clientID': self._app.identifier,
            'code': pin_code,
            'context[device][product]': self._app.name,
        })
        return self._BASE_AUTH_URL + '#?' + qs

    def check_pin(self, pin_id: int, pin_code: str) -> Optional[str]:
        headers = {'Accept': 'application/json'}
        data = {
            'code': pin_code,
            'X-Plex-Client-Identifier': self._app.identifier,
        }
        r = self._make_request('GET', f'/pins/{pin_id}', headers=headers, data=data)
        r.raise_for_status()
        info = r.json()
        return info['authToken'] if info['authToken'] else None

    def wait_for_token(self, pin_id: int, pin_code: str, check_interval_sec: int = 1) -> str:
        auth_token = self.check_pin(pin_id, pin_code)
        while not auth_token:
            sleep(check_interval_sec)
            auth_token = self.check_pin(pin_id, pin_code)
        return auth_token
