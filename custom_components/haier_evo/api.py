from __future__ import annotations
import requests
import json
import time
import threading
import uuid
import socket
import weakref
from aiohttp import web
from enum import Enum
from datetime import datetime, timezone, timedelta
from tenacity import retry, stop_after_attempt, retry_if_exception_type, wait_fixed
from websocket import WebSocketApp, WebSocket
from requests.exceptions import ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode, SWING_OFF, PRESET_NONE
from homeassistant.components.http import HomeAssistantView
from .logger import _LOGGER
from .limits import ResettableLimits
from . import config as CFG # noqa
from . import const as C # noqa
from .wm import mapping as WM # noqa


class InvalidAuth(HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidDevicesList(HomeAssistantError):
    """Error to indicate we cannot connect."""

class AuthError(HTTPError):
    pass

class AuthUserError(HTTPError):
    pass

class AuthValidationError(AuthError):
    pass

class AuthInternalError(AuthError):
    pass

class ManyRequestsError(HTTPError):
    pass


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class HaierAPI(HomeAssistantView):
    url = "/api/haier_evo"
    name = "/api:haier_evo"
    requires_auth = False

    def __init__(self) -> None:
        self.haier = None

    # noinspection PyUnusedLocal
    async def get(self, request):
        if not getattr(self.haier, "allow_http", False):
            return web.Response(text="404: Not found", status=404, content_type="text/plain")
        return self.json(self.haier.to_dict())

    async def post(self, request):
        if not getattr(self.haier, "allow_http_post", False):
            return web.Response(text="404: Not found", status=404, content_type="text/plain")
        data = await request.json()
        self.haier.send_message(json.dumps(data))
        return self.json({"result": "success"})


class AuthResponse(object):

    def __init__(self, response: requests.Response):
        self.response = response
        self.json_data = response.json() or {}
        self.data = self.json_data.get("data") or {}
        self.error = self.json_data.get("error")
        self.token = self.data.get("token") or {}

    def __getattr__(self, item):
        if hasattr(self.response, item):
            return getattr(self.response, item)
        raise AttributeError(item)

    def __repr__(self) -> str:
        return self.response.__repr__()

    def raise_for_error(self) -> None:
        if self.error and isinstance(self.error, dict):
            validation = self.error.get("validation") or {}
            if message := validation.get('refreshToken'):
                # noinspection PyTypeChecker
                raise AuthValidationError(message, response=self)
            if message := validation.get('email'):
                # noinspection PyTypeChecker
                raise AuthUserError(message, response=self)
            if message := validation.get('password'):
                # noinspection PyTypeChecker
                raise AuthUserError(message, response=self)
            if message := self.error.get("message"):
                # noinspection PyTypeChecker
                raise AuthInternalError(message, response=self)
            # noinspection PyTypeChecker
            raise AuthError(str(self.error), response=self)
        return None

    @property
    def access_token(self) -> str | None:
        assert "accessToken" in self.token, f"Bad data: refreshToken not found"
        value = self.token["accessToken"]
        assert isinstance(value, str) and value, f"Bad token: {value!r}"
        return value

    @property
    def access_expire(self) -> datetime | None:
        assert "expire" in self.token, f"Bad data: expire not found"
        value = self.token["expire"]
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")

    @property
    def refresh_token(self) -> str | None:
        assert "refreshToken" in self.token, f"Bad data: refreshToken not found"
        value = self.token["refreshToken"]
        assert isinstance(value, str) and value, f"Bad token: {value!r}"
        return value

    @property
    def refresh_expire(self) -> datetime | None:
        assert "refreshExpire" in self.token, f"Bad data: refreshExpire not found"
        value = self.token["refreshExpire"]
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


class Haier(object):

    http = HaierAPI()
    connect_limits = ResettableLimits(calls=1, period=5)
    common_limits = ResettableLimits(
        calls=C.COMMON_LIMIT_CALLS,
        period=C.COMMON_LIMIT_PERIOD,
    )
    auth_login_limits = ResettableLimits(
        calls=C.LOGIN_LIMIT_CALLS,
        period=C.LOGIN_LIMIT_PERIOD,
        max=C.LOGIN_LIMIT_MAX
    )
    auth_refresh_limits = ResettableLimits(
        calls=C.REFRESH_LIMIT_CALLS,
        period=C.REFRESH_LIMIT_PERIOD,
        max=C.REFRESH_LIMIT_MAX
    )

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        region: str,
        http: bool = C.API_HTTP_ROUTE
    ) -> None:
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pull_data = None
        self._device_id = str(uuid.uuid4())
        self.hass: HomeAssistant = hass
        self.devices: list[HaierDevice] = []
        self.email: str = email
        self.password: str = password
        self.region: str = region
        self.allow_http: bool = http
        self.allow_http_post: bool = False
        self.token: str | None = None
        self.tokenexpire: datetime | None = None
        self.refreshtoken: str | None = None
        self.refreshexpire: datetime | None = None
        self.socket_app: WebSocketApp | None = None
        self.disconnect_requested = False
        self.socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION
        self.socket_thread = None
        self.reset_limits()
        self.register_view()

    def to_dict(self) -> dict:
        return {
            "socket_status": getattr(self.socket_status, "value", None),
            "backend_data": self._pull_data,
            "devices": [device.to_dict() for device in self.devices]
        }

    def load_tokens(self) -> None:
        filename = self.hass.config.path(C.DOMAIN)
        try:
            with open(filename, "r") as f:
                data = json.load(f)
            assert isinstance(data, dict), "Bad saved tokens file"
            self.token = data.get("token", None)
            tokenexpire = data.get("tokenexpire")
            self.tokenexpire = datetime.fromisoformat(tokenexpire) if tokenexpire else None
            self.refreshtoken = data.get("refreshtoken", None)
            refreshexpire = data.get("refreshexpire")
            self.refreshexpire = datetime.fromisoformat(refreshexpire) if refreshexpire else None
            _LOGGER.info(f"Loaded tokens file: {filename}")
        except FileNotFoundError:
            _LOGGER.warning(f"No tokens file: {filename}")
        except Exception as e:
            _LOGGER.error(f"Failed to load tokens file: {e}")

    def save_tokens(self) -> None:
        try:
            filename = self.hass.config.path(C.DOMAIN)
            with open(filename, "w") as f:
                json.dump({
                    "token": self.token,
                    "tokenexpire": str(self.tokenexpire) if self.tokenexpire else None,
                    "refreshtoken": self.refreshtoken,
                    "refreshexpire": str(self.refreshexpire) if self.refreshexpire else None,
                }, f)
        except Exception as e:
            _LOGGER.error(f"Failed to save tokens file: {e}")
        else:
            _LOGGER.debug(f"Saved tokens file: {filename}")

    def clear_tokens(self) -> None:
        self.token = None
        self.tokenexpire = None
        self.refreshtoken = None
        self.refreshexpire = None
        self.save_tokens()

    def reset_limits(self) -> None:
        self.connect_limits.reset()
        self.common_limits.reset()
        self.auth_login_limits.reset()
        self.auth_refresh_limits.reset()

    def get_http_resources(self) -> list:
        http = getattr(self.hass, "http", None)
        app = getattr(http, "app", None)
        router = getattr(app, "router", None)
        resources = getattr(router, "resources", None)
        return resources() if resources else []

    def register_view(self) -> None:
        if self.http.url not in (r.canonical for r in self.get_http_resources()):
            self.hass.http.register_view(self.http)
        self.http.haier = weakref.proxy(self)

    def unregister_view(self) -> None:
        self.http.haier = None

    def stop(self) -> None:
        self.disconnect_requested = True
        self.reset_limits()
        if self.socket_app is not None:
            self.socket_app.close()
        self.unregister_view()

    @common_limits.sleep_and_retry
    @common_limits
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            assert self.disconnect_requested is False, 'Service already stoped'
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', C.API_TIMEOUT)
            headers = kwargs.setdefault('headers', {})
            headers.setdefault('User-Agent', "evo-mobile")
            headers.setdefault('Platform', "android")
            headers.setdefault('Accept', "*/*")
            resp = requests.request(method, url, **kwargs)
            # _LOGGER.debug(resp.text)
            # Handling 429 Too Many Requests with retry
            if resp.status_code == 429:
                raise ManyRequestsError("429 Too Many Requests", response=resp)
            # Raise for other HTTP errors
            resp.raise_for_status()
            return resp
        except (ConnectionError, NewConnectionError, socket.gaierror) as e:
            _LOGGER.error(f"Network error occurred: {e}")
            raise e  # Re-raise to allow retry mechanisms to handle this
        except Timeout as e:
            _LOGGER.error(f"Request timed out: {e}")
            raise e
        except HTTPError as e:
            _LOGGER.error(f"HTTP error occurred: {e}")
            raise e

    @auth_login_limits.sleep_and_retry
    @auth_login_limits
    def auth_login(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_LOGIN.format(region=self.region))
            _LOGGER.debug(f"Logging in to {path}")
            response = AuthResponse(self.make_request('POST', path, data={
                'email': self.email,
                'password': self.password
            }))
            # _LOGGER.info(f"Login status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_429)
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_500)
            response = e.response
        except AuthUserError as e:
            self.disconnect_requested = True
            raise e
        else:
            self.auth_login_limits.set_period()
        finally:
            self.auth_refresh_limits.reset()
        return response

    @auth_refresh_limits.sleep_and_retry
    @auth_refresh_limits
    def auth_refresh(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_TOKEN_REFRESH.format(region=self.region))
            _LOGGER.debug(f"Refreshing token in to {path}")
            response = AuthResponse(self.make_request('POST', path, data={
                'refreshToken': self.refreshtoken
            }))
            # _LOGGER.info(f"Refresh status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_429)
            raise e
        except AuthValidationError as e:
            _LOGGER.error(str(e))
            self.clear_tokens()
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_500)
            response = e.response
        else:
            self.auth_refresh_limits.set_period()
        finally:
            self.auth_login_limits.reset()
        return response

    @retry(
        retry=retry_if_exception_type(AuthValidationError),
        stop=stop_after_attempt(2),
    )
    def login(self, refresh: bool = False) -> None:
        resp = None
        try:
            if refresh and self.refreshtoken:  # token refresh
                resp = self.auth_refresh()
            else:  # initial login
                resp = self.auth_login()
            assert resp, "No response from login"
            self.token = resp.access_token
            self.tokenexpire = resp.access_expire
            self.refreshtoken = resp.refresh_token
            self.refreshexpire = resp.refresh_expire
            self.save_tokens()
        except AuthValidationError as e:
            raise e
        except AssertionError as e:
            _LOGGER.error(f"Assertion error: {e}")
        except Exception as e:
            _LOGGER.error(
                f"Failed to login/refresh token, "
                f"response was: {resp}, "
                f"err: {e}"
            )
            raise InvalidAuth()
        else:
            _LOGGER.debug(f"Successful update tokens")

    def auth(self) -> None:
        with self._lock:
            tzinfo = timezone(timedelta(hours=+3.0))
            # tzinfo = datetime.now(timezone.utc).astimezone().tzinfo
            now = datetime.now(tzinfo)
            tokenexpire = self.tokenexpire or now
            refreshexpire = self.refreshexpire or now
            if self.token:
                if tokenexpire > now:
                    return None
                elif self.refreshtoken and refreshexpire > now:
                    # _LOGGER.debug(f"Token to be refreshed")
                    return self.login(refresh=True)
            # _LOGGER.debug(f"Token expired or empty")
            return self.login()

    def pull_data_from_api(self) -> dict:
        self.auth()
        response = None
        try:
            devices_path = urljoin(C.API_PATH, C.API_DEVICES.format(region=self.region))
            _LOGGER.debug(f"Getting devices, url: {devices_path}")
            response = requests.get(devices_path, headers={
                'X-Auth-Token': self.token,
                'User-Agent': 'evo-mobile',
                'Platform': 'android',
                'Device-Id': self._device_id,
                'Content-Type': 'application/json'
            }, timeout=C.API_TIMEOUT)
            # _LOGGER.debug(response.text)
            response.raise_for_status()
            data = response.json().get("data", {})
            assert isinstance(data, dict), f"Data is not dict: {data}"
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get devices {e}, response was: {response}")
            return {}

    @retry(
        retry=retry_if_exception_type(HTTPError),
        stop=stop_after_attempt(2),
    )
    def pull_device_data(self, device_mac: str) -> dict:
        self.auth()
        response = None
        try:
            status_url = C.API_STATUS.format(mac=device_mac)
            _LOGGER.debug(f"Getting initial status of device {device_mac}, url: {status_url}")
            response = requests.get(status_url, headers={
                'X-Auth-Token': self.token,
                'User-Agent': 'evo-mobile',
                'Platform': 'android',
                'Device-Id': self._device_id,
                'Content-Type': 'application/json'
            }, timeout=C.API_TIMEOUT)
            # _LOGGER.debug(f"Update device {device_mac} status code: {response.status_code}")
            # _LOGGER.debug(response.text)s
            response.raise_for_status()
            data = response.json()
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get status: {e}, response was: {response}")
            raise

    def pull_data(self) -> None:
        self._pull_data = data = self.pull_data_from_api()
        if not self._pull_data:
            raise InvalidDevicesList()
        need_container_id = "72a6d224-cb66-4e6d-b427-2e4609252684"
        presentation = data.setdefault("presentation", {})
        layout = presentation.setdefault("layout", {})
        containers = layout.setdefault("scrollContainer", [])
        for item in containers[:]:
            tracking_data = item.setdefault("trackingData", {})
            component = tracking_data.setdefault("component", {})
            component_id = component.setdefault("componentId", "")
            # _LOGGER.debug(component_id)
            component_name = component.setdefault("componentName", "")
            if not (
                component_name == "deviceList"
                and component_id == need_container_id
            ):
                containers.remove(item)
                continue
            state_data = item.setdefault("state", "{}")
            state_json = item['state'] = (
                json.loads(state_data)
                if isinstance(state_data, str)
                else state_data
            )
            devices = state_json.setdefault("items", [])
            for d in devices:
                device_title = d.get('title', '')
                device_link = d.get('action', {}).get('link', '')
                parsed_link = urlparse(device_link)
                query_params = parse_qs(parsed_link.query)
                device_type = query_params.setdefault('type', ['UNKNOWN'])[0]
                device_mac = query_params.get('deviceId', [''])[0]
                device_mac = device_mac.replace('%3A', ':')
                device_serial = query_params.get('serialNum', [''])[0]
                device = HaierDevice.create(
                    haier=self,
                    device_type=device_type,
                    device_mac=device_mac,
                    device_serial=device_serial,
                    device_title=device_title,
                )
                self.devices.append(device)
                _LOGGER.info(f"Added device: {device}")
        if len(self.devices) > 0:
            self.connect_in_thread()

    def get_device_by_id(self, id_: str) -> HaierDevice | None:
        return next(filter(
            lambda d: d.device_id == id_,
            self.devices
        ), None)

    def _init_ws(self) -> None:
        self.auth()
        url = urljoin(C.API_WS_PATH, self.token)
        if self.socket_app is None:
            self.socket_app = WebSocketApp(
                url=url,
                on_message=self._on_message,
                on_open=self._on_open,
                on_ping=self._on_ping,
                on_close=self._on_close,
            )
        else:
            self.socket_app.url = url

    # noinspection PyUnusedLocal
    def _on_message(self, ws: WebSocket, message: str) -> None:
        _LOGGER.debug(f"Received WSS message: {message}")
        message_dict: dict = json.loads(message)
        message_device = str(message_dict.get("macAddress")).lower()
        device = self.get_device_by_id(message_device)
        if device is None:
            _LOGGER.error(f"Got a message for a device we don't know about: {message_device}")
        else:
            device.on_message(message_dict)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_open(self, ws: WebSocket) -> None:
        self.socket_status = SocketStatus.INITIALIZED
        _LOGGER.debug("Websocket opened")
        for device in self.devices:
            device.init_if_needed()

    # noinspection PyUnusedLocal
    def _on_ping(self, ws: WebSocket) -> None:
        self.socket_app.sock.pong()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_close(self, ws: WebSocket, close_code: int, close_message: str) -> None:
        _LOGGER.debug(f"Websocket closed. Code: {close_code}, message: {close_message}")

    def _wait_websocket(self, timeout: float) -> None:
        current = time.time()
        while time.time() <= (current + timeout):
            if self.socket_status == SocketStatus.INITIALIZED:
                return
            time.sleep(0.1)

    def write_ha_state(self) -> None:
        for device in self.devices:
            device.write_ha_state()

    def connect_if_needed(self, timeout: float = 4.0) -> None:
        if self.socket_thread and self.socket_thread.is_alive():
            return self._wait_websocket(timeout)
        return self.connect_in_thread()

    def connect(self) -> None:
        self.socket_status = SocketStatus.NOT_INITIALIZED
        while not self.disconnect_requested:
            self.run_forever()
        _LOGGER.debug("Connection stoped")

    def connect_in_thread(self) -> None:
        self.socket_thread = thread = threading.Thread(target=self.connect)
        thread.daemon = True
        thread.start()

    @connect_limits.sleep_and_retry
    @connect_limits
    def run_forever(self) -> None:
        _LOGGER.debug(f"Connecting to websocket ({C.API_WS_PATH})")
        try:
            self.socket_status = SocketStatus.INITIALIZING
            self._init_ws()
            self.socket_app.run_forever(ping_interval=10)
        except Exception as e:
            _LOGGER.error(f"Error connecting to websocket: {e}")

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(2),
        retry_error_callback=lambda retry_state: _LOGGER.error(
            f"Failed to send WS message after {retry_state.attempt_number} attempts: "
            f"{retry_state.outcome.exception()}"
        ),
        wait=wait_fixed(1.0),
    )
    def send_message(self, payload: str) -> None:
        _LOGGER.debug(f"Sending message: {payload}")
        with self._send_lock:
            # Fail fast: do not try to send through a closed or not-yet-open websocket.
            if self.socket_status != SocketStatus.INITIALIZED:
                _LOGGER.warning("Socket not ready before send, reconnecting...")
                self.connect_if_needed()
                if self.socket_status != SocketStatus.INITIALIZED:
                    raise ConnectionError("Socket not ready after reconnect attempt")
            try:
                self.socket_app.send(payload)
            except Exception as e:
                _LOGGER.warning(f"Failed to send message: {e}")
                self.connect_if_needed()
                raise e
            # If the socket dies immediately after send, the command can be lost.
            # A short delay catches abrupt closes and lets tenacity resend once.
            time.sleep(0.06)
            if self.socket_status != SocketStatus.INITIALIZED:
                _LOGGER.warning("Socket closed immediately after send — message likely lost, retrying...")
                self.connect_if_needed()
                raise ConnectionError("Socket closed right after send")


class HaierDevice(object):

    def __init__(
        self,
        haier: Haier,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
        backend_data: dict = None,
    ) -> None:
        self._haier = weakref.proxy(haier)
        self.device_id = device_mac
        self.device_serial = device_serial
        self.device_name = device_title
        self.device_model = "UNKNOWN"
        self.sw_version = None
        self._write_ha_state_callbacks = []
        self._available = True
        self._config = None
        self._status_data = backend_data

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"{self.device_id!r},"
            f"name={self.device_name!r},"
            f"serial={self.device_serial!r},"
            f"model={self.device_model!r},"
            f"config={self.config!r}"
            f")"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(C.DOMAIN, self.device_id)},
            name=self.device_name,
            sw_version=self.sw_version,
            model=self.device_model,
            manufacturer="Haier"
        )

    @property
    def device_mac(self) -> str:
        return self.device_id

    @property
    def available(self) -> bool:
        # return self._available
        # this works very bad
        return True

    @available.setter
    def available(self, value: bool | str):
        if not isinstance(value, bool):
            self._available = False if str(value).upper() == 'OFFLINE' else True
        else:
            self._available = value

    @property
    def status_data(self) -> dict:
        return self._status_data

    @property
    def hass(self) -> HomeAssistant:
        return self._haier.hass

    @property
    def config(self) -> CFG.HaierDeviceConfig:
        return self._config

    @property
    def constraint(self) -> CFG.Constraint:
        return self.config.constraint

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self.device_serial,
            "sw_version": self.sw_version,
            "config": self.config.to_dict() if self.config else None,
            "backend_data": self.status_data,
        }

    def _get_status(self, data: dict) -> dict:
        data = (data or {})
        # Evo RU may return device details wrapped in smartDeviceControl.
        # Without this unwrap, WM devices fall back to model AC and load AC.yaml.
        if isinstance(data, dict) and isinstance(data.get("smartDeviceControl"), dict):
            data = data["smartDeviceControl"]
        self._status_data = data
        info = data.setdefault("info", {})
        self.device_serial = info.setdefault("serialNumber", self.device_serial)
        device_model = info.setdefault("model", "AC")
        device_model = device_model.replace('-','').replace('/', '')[:11]
        self.device_model = device_model
        self.available = data.setdefault("status", "ONLINE")
        settings = data.setdefault("settings", {})
        self.device_name = settings.setdefault("name", {}).setdefault("name", self.device_name)
        self.sw_version = settings.setdefault('firmware', {}).setdefault('value', None)
        # read config and current values
        self._load_config_from_attributes(data)
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass

    def _iter_property_pairs(self, obj):
        """Yield (attribute_code, value) pairs from different Evo message shapes."""
        if isinstance(obj, dict):
            # WebSocket command_response/status often contains properties as a plain dict.
            props = obj.get("properties")
            if isinstance(props, dict):
                for key, value in props.items():
                    yield str(key), value

            # Evo RU detailed/config responses contain attributes with currentValue.
            attrs = obj.get("attributes")
            if isinstance(attrs, list):
                for item in attrs:
                    if not isinstance(item, dict):
                        continue
                    code = item.get("name") or item.get("attrName")
                    if code is not None and "currentValue" in item:
                        yield str(code), item.get("currentValue")

            # ProgramWasherBaseEvent contains selectedValues.
            selected = obj.get("selectedValues")
            if isinstance(selected, list):
                for item in selected:
                    if not isinstance(item, dict):
                        continue
                    code = item.get("attrName") or item.get("name")
                    value = item.get("attrValue") if "attrValue" in item else item.get("currentValue")
                    if code is not None:
                        yield str(code), value

            for value in obj.values():
                if isinstance(value, (dict, list)):
                    yield from self._iter_property_pairs(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self._iter_property_pairs(item)

    def _handle_status_update(self, received_message: dict) -> None:
        updated = False
        pairs = list(self._iter_property_pairs(received_message))
        _LOGGER.debug("%s: status update event=%s pairs=%s", self.device_name, received_message.get("event"), pairs[:80])
        for key, value in pairs:
            self._set_attribute_value(key, value)
            updated = True
        self.available = True
        if updated:
            _LOGGER.debug("%s: status after update %s", self.device_name, self.to_dict())
        self.write_ha_state()

    def _handle_device_status_update(self, received_message: dict) -> None:
        status = received_message.get("payload", {}).get("status")
        self.available = status
        self.write_ha_state()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload", {})
        self.sw_version = payload.get("swVersion") or self.sw_version

    def _send_message(self, message: dict) -> None:
        self._haier.send_message(json.dumps(message))

    def _send_commands(self, commands: list[dict]) -> None:
        self._send_group_command(commands)

    def _send_group_command(self, commands: list[dict]) -> None:
        trace = str(uuid.uuid4())
        _ = self._send_message({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self.config.command_name,
            "commands": commands,
            "trace": trace,
        }) if self.config.command_name else [
            self._send_single_command(c)
            for c in commands
        ]

    def _send_single_command(self, command: dict) -> None:
        trace = str(uuid.uuid4())
        self._send_message({
            "action": "command",
            "macAddress": self.device_id,
            "command": command,
            "trace": trace,
        })

    def init_if_needed(self) -> None:
        pass

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        value = str({True: "on", False: "off", None: "off"}.get(value, value))
        if custom := self.config.get_command_by_name(f"{name}_{value}"):
            return custom
        attr = self.config.get_attr_by_name(name)
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code(value),
        }] if attr else [])

    def on_message(self, message_dict: dict) -> None:
        message_type = message_dict.get("event", "")
        try:
            _LOGGER.debug("%s: websocket event=%s keys=%s raw=%s", self.device_name, message_type, list(message_dict.keys()), json.dumps(message_dict, ensure_ascii=False)[:4000])
        except Exception:
            _LOGGER.debug("%s: websocket event=%s raw_unserializable=%r", self.device_name, message_type, message_dict)
        if message_type == "status":
            self._handle_status_update(message_dict)
        elif message_type == "command_response":
            err_no = message_dict.get("errNo", 0)
            if err_no not in (0, "0", None):
                _LOGGER.warning(f"Command rejected by device (errNo={err_no}): {message_dict}")
            else:
                _LOGGER.debug(f"Command response: {message_dict}")
            # Some devices include fresh status/properties in command_response.
            self._handle_status_update(message_dict)
        elif message_type == "info":
            self._handle_info(message_dict)
        elif message_type == "deviceStatusEvent":
            self._handle_device_status_update(message_dict)
        elif message_type == "ProgramWasherBaseEvent" and hasattr(self, "_handle_program_washer_event"):
            self._handle_program_washer_event(message_dict)
        else:
            _LOGGER.warning(f"Got unknown message: {message_dict}")

    def write_ha_state(self) -> None:
        for callback in self._write_ha_state_callbacks:
            self.hass.loop.call_soon_threadsafe(callback)

    def add_write_ha_state_callback(self, callback) -> None:
        if callback not in self._write_ha_state_callbacks:
            self._write_ha_state_callbacks.append(callback)

    # noinspection PyMethodMayBeStatic
    def create_entities_climate(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_switch(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_select(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_sensor(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_binary_sensor(self) -> list:
        return []

    @classmethod
    def create(
        cls,
        haier: Haier,
        device_type: str,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
    ) -> HaierDevice:
        device_cls = {
            "AC": HaierAC,
            "REF": HaierREF,
            "WM": HaierWM,
        }.get(device_type, cls)
        if device_cls is cls:
            _LOGGER.warning(f"Unknown device type: {device_type}")
        return device_cls(
            haier=haier,
            device_mac=device_mac,
            device_serial=device_serial,
            device_title=device_title,
            backend_data=haier.pull_device_data(device_mac),
        )


class HaierAC(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_temperature = 0
        self.target_temperature = 0
        self.status = None
        self.mode = None
        self.fan_mode = None
        self.swing_horizontal_mode = None
        self.swing_mode = None
        self._preset_mode = None
        self.min_temperature = 7
        self.max_temperature = 35
        self.light_on = True
        self.sound_on = True
        self.quiet_on = False
        self.turbo_on = False
        self.health_on = False
        self.comfort_on = False
        self.cleaning_on = False
        self.antifreeze_on = False
        self.autohumidity_on = False
        self.eco_sensor = None
        self._get_status(backend_data)
        self._inited = False

    @property
    def config(self) -> CFG.HaierACConfig:
        return self._config

    @property
    def preset_mode(self) -> str:
        if self._preset_mode not in ("none", "sleep", "boost"):
            return self._preset_mode
        elif self.quiet_on:
            return "sleep"
        elif self.turbo_on:
            return "boost"
        return "none"

    @preset_mode.setter
    def preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_temperature": self.current_temperature,
            "target_temperature": self.target_temperature,
            "max_temperature": self.max_temperature,
            "min_temperature": self.min_temperature,
            "status": self.status,
            "mode": self.mode,
            "fan_mode": self.fan_mode,
            "swing_horizontal_mode": self.swing_horizontal_mode,
            "swing_mode": self.swing_mode,
            "preset_mode": self.preset_mode,
            "light_on": self.light_on,
            "sound_on": self.sound_on,
            "quiet_on": self.quiet_on,
            "turbo_on": self.turbo_on,
            "health_on": self.health_on,
            "comfort_on": self.comfort_on,
            "cleaning_on": self.cleaning_on,
            "antifreeze_on": self.antifreeze_on,
            "autohumidity_on": self.autohumidity_on,
            "eco_sensor": self.eco_sensor,
        })
        return data

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "status":
            self.status = int(value)
        elif attr.name == "target_temperature":
            self.target_temperature = float(value)
        elif attr.name == "mode":
            self.mode = attr.get_item_name(value)
        elif attr.name == "fan_mode":
            self.fan_mode = attr.get_item_name(value)
        elif attr.name == "swing_horizontal_mode":
            self.swing_horizontal_mode = attr.get_item_name(value)
        elif attr.name == "swing_mode":
            self.swing_mode = attr.get_item_name(value)
        elif attr.name == "light":
            self.light_on = parsebool(attr.get_item_name(value))
        elif attr.name == "sound":
            self.sound_on = parsebool(attr.get_item_name(value))
        elif attr.name == "quiet":
            self.quiet_on = parsebool(attr.get_item_name(value))
        elif attr.name == "turbo":
            self.turbo_on = parsebool(attr.get_item_name(value))
        elif attr.name == "health":
            self.health_on = parsebool(attr.get_item_name(value))
        elif attr.name == "comfort":
            self.comfort_on = parsebool(attr.get_item_name(value))
        elif attr.name == "cleaning":
            self.cleaning_on = parsebool(attr.get_item_name(value))
        elif attr.name == "antifreeze":
            self.antifreeze_on = parsebool(attr.get_item_name(value))
        elif attr.name == "autohumidity":
            self.autohumidity_on = parsebool(attr.get_item_name(value))
        elif attr.name == "eco_sensor":
            self.eco_sensor = attr.get_item_name(value)

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierACConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        sensors = data.setdefault("sensors", {}).get("items", [])
        sensor_curr_temp = next(filter(lambda i: (
            isinstance(i, dict)
            and isinstance(i.get("value"), dict)
            and i.get("value", {}).get("description") == "indoorTemperature"
        ), sensors), {}).get("value", {}).get("name")
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            if attr.name == "current_temperature" and str(attr.code) != sensor_curr_temp:
                continue
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            if attr.name == "target_temperature":
                self.min_temperature = float(attr.range.min_value)
                self.max_temperature = float(attr.range.max_value)
            _LOGGER.debug(f"{self.device_name}: {attr}")
        self.constraint.extend(data.setdefault("constraint", []))

    def _get_status(self, data: dict) -> dict:
        data = super()._get_status(data)
        if self.swing_horizontal_mode is None:
            self.swing_horizontal_mode = SWING_OFF
        if self.swing_mode is None:
            self.swing_mode = SWING_OFF
        if self.preset_mode is None:
            self.preset_mode = PRESET_NONE
        self.write_ha_state()
        return data

    def init_if_needed(self) -> None:
        if not self._inited and next(filter(
            lambda a: (not a.name.startswith("preset_mode_") and a.current is None),
            self.config.attrs
        ), None) is not None:
            self.set_temperature(self.target_temperature)
        self._inited = True

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        if name != "preset_mode":
            return super().get_commands(name, value)
        func = getattr(self, f"get_preset_mode_{value}", None)
        if func is not None:
            return func()
        return self.get_preset_mode_command(value)

    def get_preset_mode_none(self) -> list[dict]:
        if custom := self.config.get_command_by_name('preset_mode_none'):
            return custom
        return [{
            "commandName": str(attr.code),
            "value": attr.get_item_code("off", "0"),
        } for attr in filter(
            lambda a: a.name.startswith("preset_mode"),
            self.config.attrs
        )]

    def get_preset_mode_command(self, mode: str) -> list[dict]:
        if custom := self.config.get_command_by_name(f'preset_mode_{mode}'):
            return custom
        attr = self.config.get_attr_by_name(f"preset_mode_{mode}")
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code("on", "1")
        }] if attr else [])

    def get_supported_features(self) -> ClimateEntityFeature:
        value = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.TURN_OFF |
            ClimateEntityFeature.TURN_ON |
            ClimateEntityFeature.FAN_MODE
        )
        if self.config['swing_horizontal_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_HORIZONTAL_MODE
        if self.config['swing_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_MODE
        if self.config.preset_mode is True:
            value = value | ClimateEntityFeature.PRESET_MODE
        return ClimateEntityFeature(value)

    def get_hvac_modes(self) -> list[HVACMode]:
        modes = []
        for mode in self.config.get_values('mode'):
            try:
                modes.append(HVACMode(mode))
            except ValueError:
                pass
        return modes + [HVACMode.OFF]

    def get_fan_modes(self) -> list[str]:
        return self.config.get_values('fan_mode')

    def get_swing_horizontal_modes(self) -> list[str]:
        return self.config.get_values('swing_horizontal_mode')

    def get_swing_modes(self) -> list[str]:
        return self.config.get_values('swing_mode')

    def get_preset_modes(self) -> list[str]:
        return ["none"] + self.config.get_preset_modes()

    def get_eco_sensor_options(self) -> list[str]:
        return self.config.get_values('eco_sensor')

    def set_temperature(self, value: float) -> None:
        self._send_commands([
            {
                "commandName": self.config['target_temperature'],
                "value": str(value)
            }
        ])
        self.target_temperature = value

    def _get_status_commands(self, turn_on: bool) -> list[dict]:
        """Build status on/off commands with a numeric fallback.

        Some devices/firmwares do not return a reliable label mapping for
        status. In that case get_item_code() may return None/"None", so we
        fall back to the known raw values: 1=on, 0=off.
        """
        target = "on" if turn_on else "off"
        fallback_value = "1" if turn_on else "0"
        cmds = self.get_commands("status", target)
        if status_code := self.config['status']:
            if not cmds or any(c.get("value") in (None, "None") for c in cmds):
                _LOGGER.warning(
                    f"status mapping for '{target}' not found, "
                    f"using fallback value '{fallback_value}'"
                )
                cmds = self.constraint.apply([{
                    "commandName": status_code,
                    "value": fallback_value,
                }])
        return cmds

    def switch_on(self, value: str = None) -> None:
        value = value or self.mode or HVACMode.AUTO
        self._send_commands([
            *self._get_status_commands(turn_on=True),
            *self.get_commands("mode", value),
        ])
        self.status = 1
        self.mode = value

    def switch_off(self) -> None:
        self._send_commands([
            *self._get_status_commands(turn_on=False),
        ])
        self.status = 0

    def set_fan_mode(self, value: str) -> None:
        if commands := self.get_commands("fan_mode", value):
            self._send_commands(commands)
            self.fan_mode = value

    def set_swing_horizontal_mode(self, value: str) -> None:
        if commands := self.get_commands("swing_horizontal_mode", value):
            self._send_commands(commands)
            self.swing_horizontal_mode = value

    def set_swing_mode(self, value: str) -> None:
        if commands := self.get_commands("swing_mode", value):
            self._send_commands(commands)
            self.swing_mode = value

    def set_preset_mode(self, value: str) -> None:
        if commands := self.get_commands("preset_mode", value):
            self._send_commands(commands)
            self.preset_mode = value

    def set_light_on(self, value: bool) -> None:
        if commands := self.get_commands("light", value):
            self._send_commands(commands)
            self.light_on = value

    def set_sound_on(self, value: bool) -> None:
        if commands := self.get_commands("sound", value):
            self._send_commands(commands)
            self.sound_on = value

    def set_quiet_on(self, value: bool) -> None:
        if commands := self.get_commands("quiet", value):
            self._send_commands(commands)
            self.quiet_on = value

    def set_health_on(self, value: bool) -> None:
        if commands := self.get_commands("health", value):
            self._send_commands(commands)
            self.health_on = value

    def set_turbo_on(self, value: bool) -> None:
        if commands := self.get_commands("turbo", value):
            self._send_commands(commands)
            self.turbo_on = value

    def set_comfort_on(self, value: bool) -> None:
        if commands := self.get_commands("comfort", value):
            self._send_commands(commands)
            self.comfort_on = value

    def set_cleaning_on(self, value: bool) -> None:
        if commands := self.get_commands("cleaning", value):
            self._send_commands(commands)
            self.cleaning_on = value

    def set_antifreeze_on(self, value: bool) -> None:
        if commands := self.get_commands("antifreeze", value):
            self._send_commands(commands)
            self.antifreeze_on = value

    def set_autohumidity_on(self, value: bool) -> None:
        if commands := self.get_commands("autohumidity", value):
            self._send_commands(commands)
            self.autohumidity_on = value

    def set_eco_sensor(self, value: str) -> None:
        if commands := self.get_commands("eco_sensor", value):
            self._send_commands(commands)
            self.eco_sensor = value

    def create_entities_climate(self) -> list:
        from . import climate
        return [climate.HaierACEntity(self)]
    
    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['light'] is not None:
            entities.append(switch.HaierACLightSwitch(self))
        if self.config['sound'] is not None:
            entities.append(switch.HaierACSoundSwitch(self))
        if self.config['quiet'] is not None:
            entities.append(switch.HaierACQuietSwitch(self))
        if self.config['turbo'] is not None:
            entities.append(switch.HaierACTurboSwitch(self))
        if self.config['health'] is not None:
            entities.append(switch.HaierACHealthSwitch(self))
        if self.config['comfort'] is not None:
            entities.append(switch.HaierACComfortSwitch(self))
        if self.config['cleaning'] is not None:
            entities.append(switch.HaierACCleaningSwitch(self))
        if self.config['antifreeze'] is not None:
            entities.append(switch.HaierACAntiFreezeSwitch(self))
        if self.config['autohumidity'] is not None:
            entities.append(switch.HaierACAutoHumiditySwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['eco_sensor'] is not None:
            entities.append(select.HaierACEcoSensorSelect(self))
        return entities


class HaierREF(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_fridge_temperature = 0
        self.current_freezer_temperature = 0
        self.current_temperature = 0
        self.fridge_mode = None
        self.freezer_mode = None
        self.my_zone = None
        self.super_cooling = False
        self.super_freeze = False
        self.vacation_mode = False
        self.door_open = False
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierREFConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_fridge_temperature": self.current_fridge_temperature,
            "current_freezer_temperature": self.current_freezer_temperature,
            "current_temperature": self.current_temperature,
            "fridge_mode": self.fridge_mode,
            "freezer_mode": self.freezer_mode,
            "my_zone": self.my_zone,
            "super_cooling": self.super_cooling,
            "super_freeze": self.super_freeze,
            "vacation_mode": self.vacation_mode,
            "door_open": self.door_open,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierREFConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            _LOGGER.debug(f"{self.device_name}: {attr}")

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_fridge_temperature":
            self.current_fridge_temperature = float(value)
        elif attr.name == "current_freezer_temperature":
            self.current_freezer_temperature = float(value)
        elif attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "fridge_mode":
            self.fridge_mode = attr.get_item_name(value)
        elif attr.name == "freezer_mode":
            self.freezer_mode = attr.get_item_name(value)
        elif attr.name == "my_zone":
            self.my_zone = attr.get_item_name(value)
        elif attr.name == "super_cooling":
            self.super_cooling = parsebool(attr.get_item_name(value))
        elif attr.name == "super_freeze":
            self.super_freeze = parsebool(attr.get_item_name(value))
        elif attr.name == "vacation_mode":
            self.vacation_mode = parsebool(attr.get_item_name(value))
        elif attr.name == "door_open":
            self.door_open = parsebool(attr.get_item_name(value))

    def get_fridge_mode_options(self) -> list[str]:
        return self.config.get_values('fridge_mode')

    def get_freezer_mode_options(self) -> list[str]:
        return self.config.get_values('freezer_mode')

    def get_my_zone_options(self) -> list[str]:
        return self.config.get_values('my_zone')

    def set_super_cooling(self, value: bool) -> None:
        if commands := self.get_commands("super_cooling", value):
            self._send_single_command(commands[0])
            self.super_cooling = value

    def set_super_freeze(self, value: bool) -> None:
        if commands := self.get_commands("super_freeze", value):
            self._send_single_command(commands[0])
            self.super_freeze = value

    def set_vacation_mode(self, value: bool) -> None:
        if commands := self.get_commands("vacation_mode", value):
            self._send_single_command(commands[0])
            self.vacation_mode = value

    def set_fridge_mode(self, value: str) -> None:
        if commands := self.get_commands("fridge_mode", value):
            self._send_single_command(commands[0])
            self.fridge_mode = value

    def set_freezer_mode(self, value: str) -> None:
        if commands := self.get_commands("freezer_mode", value):
            self._send_single_command(commands[0])
            self.freezer_mode = value

    def set_my_zone(self, value: str) -> None:
        if commands := self.get_commands("my_zone", value):
            self._send_single_command(commands[0])
            self.my_zone = value

    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(switch.HaierREFSuperCoolingSwitch(self))
        if self.config['super_freeze'] is not None:
            entities.append(switch.HaierREFSuperFreezeSwitch(self))
        if self.config['vacation_mode'] is not None:
            entities.append(switch.HaierREFVacationSwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['fridge_mode'] is not None:
            entities.append(select.HaierREFFridgeModeSelect(self))
        if self.config['freezer_mode'] is not None:
            entities.append(select.HaierREFFreezerModeSelect(self))
        if self.config['my_zone'] is not None:
            entities.append(select.HaierREFMyZoneSelect(self))
        return entities


    def _wm_sensor_available(self, name: str) -> bool:
        """Return True when a WM sensor can be useful.

        Older code created WM entities only when the model YAML contained the
        exact named attribute. For unknown/partially detected models this meant
        that one missing mapping could hide otherwise valid values.  Now we
        create sensors per field when either a config mapping exists or the
        value was already discovered from the device data/autodetect fallback.
        """
        try:
            if self.config[name] is not None:
                return True
        except Exception:
            pass
        return getattr(self, name, None) not in (None, "None", "unknown")

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['current_temperature'] is not None:
            entities.append(sensor.HaierREFTemperatureSensor(self))
        if self.config['current_fridge_temperature'] is not None:
            entities.append(sensor.HaierREFFridgeTemperatureSensor(self))
        if self.config['current_freezer_temperature'] is not None:
            entities.append(sensor.HaierREFFreezerTemperatureSensor(self))
        if self.config['fridge_mode'] is not None:
            entities.append(sensor.HaierREFFridgeModeSensor(self))
        if self.config['freezer_mode'] is not None:
            entities.append(sensor.HaierREFFreezerModeSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(binary_sensor.HaierREFSuperCoolingSensor(self))
        if self.config['super_freeze'] is not None:
            entities.append(binary_sensor.HaierREFSuperFreezeSensor(self))
        if self.config['vacation_mode'] is not None:
            entities.append(binary_sensor.HaierREFVacationSensor(self))
        if self.config['door_open'] is not None:
            entities.append(binary_sensor.HaierREFDoorSensor(self))
        return entities


class HaierWM(HaierDevice):

    PROGRAM_NAMES = WM.PROGRAM_NAMES
    PANEL_PROGRAM_NAMES = WM.PANEL_PROGRAM_NAMES
    STATUS_NAMES = WM.STATUS_NAMES
    PHASE_NAMES = WM.PHASE_NAMES

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.status = None
        self.program = None
        self.program_code = None
        self.program_title = None
        self.selected_program = None
        self.selected_program_code = None
        self._last_program_display = None
        self._last_selected_program_display = None
        self.temperature = None
        self.spin_speed = None
        # Backward-compatible aggregate; program_remaining_time is the total program
        # countdown, i_time is the i-Time value from code 51.
        self.remaining_time = None
        self.program_remaining_time = None
        self.i_time = None
        self.program_duration = None
        # Fixed duration for the currently selected/started program. Codes 32+33
        # are the washer display time: before start they show calculated duration,
        # after start they become the countdown. Keep program_duration fixed once
        # the washer enters an active phase; program_remaining_time continues to
        # follow the live countdown.
        self._base_program_duration_minutes = None
        self._program_duration_locked = False
        self.program_time_hours = None
        self.program_time_minutes = None
        self.program_summary = None
        # Keep the program description stable while a wash cycle is running.
        # Timers can update every minute, but the description sensor should not
        # change on each countdown tick.
        self._frozen_program_summary = None
        self.energy = None
        self.power = None
        # Code 39 is exposed as diagnostic "Расход воды" in liters while
        # we continue collecting data from different washer models.
        self.water_raw = None
        self.raw_31 = None
        self.raw_33 = None
        self.raw_34 = None
        self.raw_25 = None
        self.raw_35 = None
        self.drum_clean_wash_count = None
        self.total_wash_count = None
        # Backward-compatible alias for older code/exports.
        self.wash_count = None
        self.raw_36 = None
        self.raw_95 = None
        self.raw_195 = 0
        self.program_progress = None
        self.rinse_count = None
        self.dirt_level = None
        self.steam_function = None
        self.delayed_start_enabled = None
        self.child_lock = None
        self.sound_notification = None
        self.remote_control = None
        self.standby_mode = None
        self.delayed_start_hours = None
        self.delayed_start_minutes = None
        self.anti_crease = None
        self.raw_46 = None
        self.raw_47 = None
        self.raw_51 = None
        self.raw_61 = None
        self.raw_68 = None
        self.raw_88 = None
        self.raw_89 = None
        self.raw_91 = None
        self.raw_94 = None
        self.raw_117 = None
        self.raw_205 = None
        self.phase = None
        self.phase_code = None
        self.door_lock = None
        # Code 31 is the physical door open/closed flag on HW70/HW90.
        self.door_open = None
        self._get_status(backend_data)
        self._inited = False

    @property
    def config(self) -> CFG.HaierWMConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "status": self.status,
            "program": self.program,
            "program_code": self.program_code,
            "program_title": self.program_title,
            "selected_program": self.selected_program,
            "selected_program_code": self.selected_program_code,
            "temperature": self.temperature,
            "spin_speed": self.spin_speed,
            "remaining_time": self.remaining_time,
            "program_remaining_time": self.program_remaining_time,
            "i_time": self.i_time,
            "program_duration": self.program_duration,
            "program_time_hours": self.program_time_hours,
            "program_time_minutes": self.program_time_minutes,
            "program_summary": self.program_summary,
            "energy": self.energy,
            "power": self.power,
            "water_raw": self.water_raw,
            "raw_31": self.raw_31,
            "raw_33": self.raw_33,
            "raw_34": self.raw_34,
            "raw_25": self.raw_25,
            "raw_35": self.raw_35,
            "drum_clean_wash_count": self.drum_clean_wash_count,
            "total_wash_count": self.total_wash_count,
            "wash_count": self.wash_count,
            "raw_36": self.raw_36,
            "raw_95": self.raw_95,
            "raw_195": self.raw_195,
            "program_progress": self.program_progress,
            "rinse_count": self.rinse_count,
            "dirt_level": self.dirt_level,
            "steam_function": self.steam_function,
            "delayed_start_enabled": self.delayed_start_enabled,
            "child_lock": self.child_lock,
            "sound_notification": self.sound_notification,
            "remote_control": self.remote_control,
            "standby_mode": self.standby_mode,
            "delayed_start_hours": self.delayed_start_hours,
            "delayed_start_minutes": self.delayed_start_minutes,
            "anti_crease": self.anti_crease,
            "raw_46": self.raw_46,
            "raw_47": self.raw_47,
            "raw_51": self.raw_51,
            "raw_61": self.raw_61,
            "raw_68": self.raw_68,
            "raw_88": self.raw_88,
            "raw_89": self.raw_89,
            "raw_91": self.raw_91,
            "raw_94": self.raw_94,
            "raw_117": self.raw_117,
            "raw_205": self.raw_205,
            "phase": self.phase,
            "phase_code": self.phase_code,
            "door_lock": self.door_lock,
            "door_open": self.door_open,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWMConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        self._apply_wm_autodetect()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            _LOGGER.debug(f"{self.device_name}: {attr}")

    def _attr_values(self, attr) -> set[str]:
        try:
            return {str(i.value) for i in attr.list if i.value not in (None, "")}
        except Exception:
            return set()

    def _has_named_attr(self, name: str) -> bool:
        return self.config.get_attr_by_name(name) is not None

    def _code_has_named_attr(self, code: str) -> bool:
        return any(str(a.code) == str(code) and a.name != "unknown" for a in self.config.attrs)

    def _rename_raw_attr(self, code: str, name: str) -> bool:
        if self._has_named_attr(name):
            return False
        # Prefer an API/raw attribute with the requested code. If the model YAML
        # already contains a named attribute, _has_named_attr() above keeps it.
        attr = next((a for a in self.config.attrs if str(a.code) == str(code)), None)
        if not attr:
            return False
        old_name = attr.name
        attr.name = name
        self.config._attrs_cache.pop(old_name, None)
        self.config._attrs_cache.pop(name, None)
        _LOGGER.debug("%s: WM autodetect %s -> code %s", self.device_name, name, code)
        return True

    def _find_list_attr_by_values(self, required: set[str], *, exclude_codes: set[str] | None = None):
        exclude_codes = exclude_codes or set()
        best = None
        best_score = 0
        for attr in self.config.attrs:
            code = str(attr.code)
            if code in exclude_codes or code == "-1":
                continue
            values = self._attr_values(attr)
            if not values:
                continue
            score = len(values & required)
            if score > best_score:
                best = attr
                best_score = score
        return best if best_score >= max(3, min(len(required), 5)) else None

    def _find_program_attr(self):
        # Current/running program is a long LIST, but it is not the physical knob
        # selector (code 0) and not the phase list (code 18). Known models use 71/69.
        for code in ("71", "69"):
            attr = next((a for a in self.config.attrs if str(a.code) == code), None)
            if attr and len(self._attr_values(attr)) >= 10:
                return attr
        candidates = []
        for attr in self.config.attrs:
            code = str(attr.code)
            if code in {"0", "18", "48", "50", "61", "63"} or code == "-1":
                continue
            values = self._attr_values(attr)
            if len(values) >= 20:
                candidates.append((len(values), attr))
        return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1] if candidates else None

    def _apply_wm_autodetect(self) -> None:
        """Conservative fallback for unknown WM models.

        This only fills missing fields, so known YAML profiles stay authoritative.
        It lets similar washers expose at least core sensors without adding a full
        model profile for every code shift.
        """
        # Stable/common codes seen on WM_BASE models.
        for code, name in (("0", "selected_program"), ("9", "sound_notification"), ("14", "child_lock"), ("18", "phase"), ("21", "door_lock"), ("24", "remote_control"), ("25", "standby_mode"), ("35", "drum_clean_wash_count"), ("36", "total_wash_count"), ("38", "power"), ("40", "energy"), ("39", "water_raw")):
            self._rename_raw_attr(code, name)

        # Time shown on the washer display is split into hours (code 32) and
        # minutes (code 33) on HW70/HW90. Older/other models may still expose a
        # single remaining-time field via their YAML profile.
        if not self._has_named_attr("program_remaining_time"):
            for code in ("33", "32"):
                if self._rename_raw_attr(code, "program_remaining_time"):
                    break

        # Keep useful unknown WM codes as disabled diagnostic sensors. This does
        # not affect known fields and helps add new models without asking users
        # for huge dumps every time.
        for code in ("15", "22", "31", "33", "34", "35", "36", "37", "45", "46", "47", "51", "59", "61", "62", "68", "88", "89", "91", "94", "95", "117", "195", "205"):
            if not self._code_has_named_attr(code):
                self._rename_raw_attr(code, f"raw_{code}")

        # Temperature list normally contains 0/10/20/.../100.
        if not self._has_named_attr("temperature"):
            temp_values = {str(i) for i in range(0, 101, 10)}
            attr = self._find_list_attr_by_values(temp_values, exclude_codes={"0", "18", "69", "71"})
            if attr:
                self._rename_raw_attr(str(attr.code), "temperature")

        # Spin list normally contains 0/200/400/.../1600.
        if not self._has_named_attr("spin_speed"):
            spin_values = {"0", "200", "400", "600", "800", "1000", "1200", "1400", "1600"}
            attr = self._find_list_attr_by_values(spin_values, exclude_codes={"0", "18", "48", "50", "69", "71"})
            if attr:
                self._rename_raw_attr(str(attr.code), "spin_speed")

        if not self._has_named_attr("program"):
            attr = self._find_program_attr()
            if attr:
                self._rename_raw_attr(str(attr.code), "program")

    def _clean_value(self, value):
        return WM.clean_value(value)

    def _as_number(self, value):
        return WM.as_number(value)

    def _get_named_attr_by_code(self, code: str):
        """Return a YAML-defined attribute for code, preferring it over API 'unknown'.

        Evo returns every raw property as an attribute named 'unknown'. The YAML then
        adds a second attribute with the same numeric code and a Home Assistant name
        such as 'temperature'.  get_attr_by_code() returns the first match, which is
        often the raw 'unknown' attribute; this helper deliberately prefers the
        named YAML attribute so current values from raw codes are copied to HA fields.
        """
        if not self.config:
            return None
        matches = [a for a in self.config.attrs if str(a.code) == str(code)]
        return next((a for a in matches if a.name != "unknown"), matches[0] if matches else None)

    def _fallback_name_by_code(self, code: str) -> str | None:
        return WM.fallback_name_by_code(code)

    def _program_name_from_code(self, value):
        return WM.program_name_from_code(value)

    def _display_program_value(self, value):
        return WM.display_program_value(value)

    def _program_display_to_config_value(self, value):
        return WM.program_display_to_config_value(value)

    def _map_wm_value(self, name: str, value):
        if name == "program":
            self.program_code = str(value) if value is not None else None
        return WM.map_value(name, value)

    def _is_active_phase(self) -> bool:
        return WM.is_active_phase(self.phase_code)

    def _is_finished_phase(self) -> bool:
        return WM.is_finished_phase(self.phase_code)

    def _is_delay_enabled_value(self, value=None) -> bool:
        if value is None:
            value = self.delayed_start_enabled
        return str(value) in ("1", "Включено", "Да", "true", "True")

    def _is_delay_disabled_value(self, value=None) -> bool:
        if value is None:
            value = self.delayed_start_enabled
        return str(value) in ("0", "Выключено", "Нет", "false", "False")

    def _is_standby_mode_enabled(self) -> bool:
        return str(self.standby_mode) in ("0", "Включено", "Да", "true", "True")

    def _is_off_program_selected(self) -> bool:
        """Return True when the washer selector/program is explicitly in Off mode.

        Off must have priority over standby: on these washers code 25 can still
        report standby/sleep while the physical selector code 0 is "Выключено".
        Also handle stale Home Assistant program-select calls, where "Выключено"
        is set through the old program entity instead of arriving from code 0.
        """
        off_values = ("0", "Выключено", "Выключен", "Выключена", "off", "Off", "OFF")
        return (
            str(self.selected_program_code) == "0"
            or str(self.program_code) == "0"
            or str(self.selected_program) in off_values
            or str(self.program) in off_values
            or str(self._last_selected_program_display) in off_values
            or str(self._last_program_display) in off_values
        )

    def _refresh_program_time(self) -> None:
        """Refresh program time directly from display fields 32/33.

        Codes 32 and 33 are exposed as the washer display time, exactly like the
        original integration behavior. Delayed-start filtering/caching is not
        applied here.
        """
        hours = self._as_number(self.program_time_hours)
        minutes = self._as_number(self.program_time_minutes)
        if hours is None:
            hours = self._as_number(self.delayed_start_hours) or 0
        if minutes is None:
            minutes = self._as_number(self.delayed_start_minutes) or 0

        display_minutes = int(hours * 60 + minutes)
        self.program_remaining_time = display_minutes
        self.remaining_time = self.program_remaining_time

        if self._is_active_phase():
            if not self._program_duration_locked:
                if self.program_duration in (None, "None", "unknown"):
                    self.program_duration = display_minutes
                self._base_program_duration_minutes = self.program_duration
                self._program_duration_locked = True
        else:
            self._program_duration_locked = False
            self._base_program_duration_minutes = display_minutes
            self.program_duration = display_minutes

    def _refresh_delayed_start_time(self) -> None:
        self._refresh_program_time()


    def _refresh_program_summary(self) -> None:
        if self._is_off_program_selected():
            self._frozen_program_summary = None
            self.program_summary = "Выключено"
            return

        # During an active wash cycle keep the description unchanged. Codes 32/33
        # may change every minute, and without this guard Home Assistant records a
        # new state for the description on every countdown update.
        if self._is_active_phase() and self._frozen_program_summary not in (None, "", "None", "unknown"):
            self.program_summary = self._frozen_program_summary
            return

        if self._is_standby_mode_enabled():
            self._frozen_program_summary = None
            self.program_summary = "Режим ожидания"
            return

        parts = []
        program = self.selected_program or self.program
        if program not in (None, "None", "unknown"):
            parts.append(str(program))
        if self.temperature not in (None, "None", "unknown"):
            try:
                temp = int(float(self.temperature))
            except Exception:
                temp = self.temperature
            parts.append(f"{temp}°C" if str(temp) != "0" else "Холодная")
        if self.spin_speed not in (None, "None", "unknown"):
            try:
                spin = int(float(self.spin_speed))
            except Exception:
                spin = self.spin_speed
            parts.append(f"{spin} об/мин")
        if self.program_duration not in (None, "None", "unknown"):
            try:
                minutes = int(float(self.program_duration))
                hours, mins = divmod(minutes, 60)
                if hours and mins:
                    duration = f"{hours} ч {mins:02d} мин"
                elif hours:
                    duration = f"{hours} ч"
                else:
                    duration = f"{mins} мин"
            except Exception:
                duration = str(self.program_duration)
            parts.append(duration)
        if str(self.steam_function) in ("Да", "Включено", "1", "true", "True"):
            parts.append("Пар")
        if str(self.anti_crease) in ("Да", "Включено", "1", "true", "True"):
            parts.append("Антисминание")
        if self.rinse_count not in (None, "None", "unknown"):
            parts.append(f"Полоскания: {self.rinse_count}")
        summary = " • ".join(parts) if parts else None
        self.program_summary = summary
        if self._is_active_phase() and summary not in (None, "", "None", "unknown"):
            self._frozen_program_summary = summary

    def _refresh_derived_wm_state(self) -> None:
        if not self._is_active_phase():
            self._frozen_program_summary = None
        elif self._frozen_program_summary in (None, "", "None", "unknown") and self.program_summary not in (None, "", "None", "unknown", "Режим ожидания", "Выключено"):
            self._frozen_program_summary = self.program_summary

        self._refresh_program_summary()
        if self._is_off_program_selected():
            self.status = "Выключено"
            self.program = "Выключено"
            self.selected_program = "Выключено"
            self.program_summary = "Выключено"
            self.phase = "Нет"
            self._program_duration_locked = False
            self.program_remaining_time = 0
            self.remaining_time = 0
            self.power = 0
            return
        # Haier RU sometimes reports code 67 as 1 ("Ожидание") while the
        # washer is clearly running.  Code 18 is a more reliable source.
        status_text = str(self.status) if self.status is not None else None
        finished_by_status = status_text in ("Завершено", "FINISHED", "COMPLETED")
        if self._is_finished_phase() or finished_by_status:
            self._frozen_program_summary = None
            self.status = "Завершено"
            self.program_remaining_time = 0
            self.remaining_time = 0
            self.power = 0
            # Status says the cycle is complete. The phase should explain what
            # the user can do now instead of duplicating "Завершено".
            if self.door_lock == "Разблокирована":
                self.phase = "Ожидает выгрузки белья"
            else:
                self.phase = "Завершение"
            return

        if self._is_active_phase():
            self.status = "Выполняется"
            # Keep the i-Time value independent. Codes 32+33 are the washer display
            # time and are already combined into program_duration/program_remaining_time.
            # Code 51 stays experimental for the current stage/i-Time observation.
        else:
            # Phase code 0 is not a washing stage. It means the washer is idle /
            # waiting for a command. Reset live-only values instead of keeping the
            # last values from a finished program.
            if self.phase_code == 0:
                self.status = "Ожидание"
                self.phase = "Нет"
                self._program_duration_locked = False
                self.program_remaining_time = 0
                self.power = 0

        self.remaining_time = self.program_remaining_time

    def _set_attribute_value(self, code: str, value: str) -> None:
        raw_value = value
        value = self._clean_value(value)
        if value is None:
            return

        code = str(code)

        # i-Time on HW70/HW90 is raw property code 51. The Evo payload may also
        # contain a second named attribute (i_time) with the same code and
        # current=None, so handle the raw code directly before normal duplicate
        # attribute mapping. This mirrors how 32/33 are stored from their raw
        # values, but keeps i-Time independent from program time.
        if code == "51":
            number = self._as_number(value)
            if number is not None:
                self.raw_51 = number
                self.i_time = number
                self._refresh_derived_wm_state()
                _LOGGER.debug(
                    "%s: WM attr code=%s name=i_time raw=%r stored=%r",
                    self.device_name, code, raw_value, self.i_time
                )
            return

        attr = self._get_named_attr_by_code(code)
        name = (attr.name if attr and attr.name != "unknown" else None) or self._fallback_name_by_code(code)
        # Code 195 was previously treated as selected-cycle duration, but HW70
        # observations have not confirmed its meaning. Keep it diagnostic until
        # it is understood. Program duration is now calculated from codes 32+33.
        if code == "195":
            name = "raw_195"
        if not name:
            return

        # For text/status fields map from the raw Haier code.  The YAML mapping
        # may already return values like "Авто" or "Стирка"; feeding those back
        # into _map_wm_value produced states such as "Программа Авто" and
        # "Этап Стирка".  Numeric fields can still use the YAML value.
        mapped = attr.get_item_name(value, value) if attr and attr.name != "unknown" else value
        # Use raw numeric codes for numeric sensors.  YAML display strings like
        # "cold" or "Выключена" are useful for selects but break numeric sensors.
        numeric_value = value if name in WM.NUMERIC_FIELDS else mapped
        display = self._map_wm_value(name, value if name in WM.TEXT_FIELDS else mapped)

        if name == "status":
            self.status = "Выключено" if self._is_off_program_selected() else display
        elif name == "program":
            program_display = self.program_title or display
            self._last_program_display = program_display
            if self._is_off_program_selected() or str(program_display) in ("0", "Выключено", "Выключен", "Выключена", "off", "Off", "OFF"):
                self.program = "Выключено"
                self.status = "Выключено"
                self.program_summary = "Выключено"
            else:
                self.program = "Режим ожидания" if self._is_standby_mode_enabled() else program_display
        elif name == "selected_program":
            self.selected_program_code = str(value)
            self._last_selected_program_display = display
            if self._is_off_program_selected() or str(display) in ("0", "Выключено", "Выключен", "Выключена", "off", "Off", "OFF"):
                self.selected_program = "Выключено"
                self.program = "Выключено"
                self.status = "Выключено"
                self.program_summary = "Выключено"
            else:
                self.selected_program = "Режим ожидания" if self._is_standby_mode_enabled() else display
        elif name == "temperature":
            self.temperature = self._as_number(numeric_value)
        elif name == "spin_speed":
            self.spin_speed = self._as_number(numeric_value)
        elif name in ("remaining_time", "program_remaining_time", "i_time", "delayed_start_hours", "program_duration"):
            number = self._as_number(numeric_value)
            if number is not None:
                if name == "delayed_start_hours" or code == "32":
                    self.delayed_start_hours = number
                    self.program_time_hours = number
                    self._refresh_program_time()
                elif code == "33" or name == "program_remaining_time":
                    self.raw_33 = number
                    self.delayed_start_minutes = number
                    self.program_time_minutes = number
                    self._refresh_program_time()
                elif name == "program_duration":
                    self.program_duration = number
                elif code == "51" or name == "i_time":
                    # ID 51 is i-Time. The Evo payload often contains two attributes
                    # with code 51: raw/unknown with the current value and named
                    # i_time with current=None. Always keep the numeric value from
                    # the raw code here and do not overwrite it from derived state.
                    self.i_time = number
                else:
                    self.remaining_time = number

        elif name == "energy":
            self.energy = self._as_number(numeric_value)
        elif name == "power":
            self.power = self._as_number(numeric_value)
        elif name == "water_raw":
            water = self._as_number(numeric_value)
            self.water_raw = water * 100 if water is not None else None
        elif name in ("drum_clean_wash_count", "wash_count"):
            self.drum_clean_wash_count = self._as_number(numeric_value)
            self.wash_count = self.drum_clean_wash_count
        elif name == "total_wash_count":
            self.total_wash_count = self._as_number(numeric_value)
        elif name == "program_progress":
            self.program_progress = self._as_number(numeric_value)
        elif name == "rinse_count":
            self.rinse_count = self._as_number(numeric_value)
        elif name == "dirt_level":
            self.dirt_level = display
        elif name == "steam_function":
            self.steam_function = display
        elif name == "delayed_start_enabled":
            new_raw_15 = self._as_number(numeric_value)
            self.delayed_start_enabled = display
            self.raw_15 = new_raw_15
        elif name == "child_lock":
            self.child_lock = display
            self.raw_14 = self._as_number(numeric_value)
        elif name == "sound_notification":
            self.sound_notification = display
            self.raw_9 = self._as_number(numeric_value)
        elif name == "remote_control":
            self.remote_control = display
            self.raw_24 = self._as_number(numeric_value)
        elif name == "standby_mode":
            self.standby_mode = display
            self.raw_25 = self._as_number(numeric_value)
            if self._is_off_program_selected():
                self.program = "Выключено"
                self.selected_program = "Выключено"
            elif self._is_standby_mode_enabled():
                self.program = "Режим ожидания"
                self.selected_program = "Режим ожидания"
            else:
                if self._last_program_display not in (None, "None", "unknown"):
                    self.program = self._last_program_display
                if self._last_selected_program_display not in (None, "None", "unknown"):
                    self.selected_program = self._last_selected_program_display
        elif name == "anti_crease":
            self.anti_crease = display
            self.raw_59 = self._as_number(numeric_value)
        elif name.startswith("raw_"):
            # Raw diagnostics must expose the numeric current value from Haier.
            # Some Evo lists label both 0 and 1 as "unknown"; using the mapped
            # display value would make HA show unknown even when the raw code changes.
            raw_number = self._as_number(value)
            setattr(self, name, raw_number)
            if name == "raw_31":
                # Confirmed on HW70: 0 = door closed, 1 = door open.
                self.door_open = raw_number

        elif name == "phase":
            phase_number = self._as_number(value)
            self.phase_code = phase_number
            if phase_number is not None:
                self.phase = WM.map_value("phase", str(int(phase_number)))
            else:
                self.phase = display
        elif name == "door_lock":
            self.door_lock = display

        self._refresh_derived_wm_state()

        _LOGGER.debug(
            "%s: WM attr code=%s name=%s raw=%r display=%r stored=%r",
            self.device_name, code, name, raw_value, display if name in ("status", "program", "phase", "door_lock") else mapped, getattr(self, name, None)
        )

    def _handle_program_washer_event(self, message_dict: dict) -> None:
        program = message_dict.get("data", {}).get("program", {}) or {}
        pairs = list(self._iter_property_pairs(program))
        _LOGGER.debug(
            "%s: washer event title=%r status=%r pairs=%s",
            self.device_name, program.get("title"), program.get("status"), pairs[:80]
        )
        if title := program.get("title"):
            self.program_title = title
            self.program = title
            if self.program_code:
                self.PROGRAM_NAMES[str(self.program_code)] = title
        if status := program.get("status"):
            self.status = self._map_wm_value("status", status)
        for key, value in pairs:
            self._set_attribute_value(key, value)
        self.available = True
        self.write_ha_state()

    def get_program_options(self) -> list[str]:
        options = [self._display_program_value(v) for v in self.config.get_values('program')]
        if self.program_title and self.program_title not in options:
            options.insert(0, self.program_title)
        return list(dict.fromkeys([v for v in options if v not in (None, "", "unknown")]))

    def get_temperature_options(self) -> list[str]:
        options = []
        for value in self.config.get_values('temperature'):
            if value in (None, "", "unknown"):
                continue
            options.append("Холодная" if str(value).lower() in ("cold", "0") else str(value))
        return list(dict.fromkeys(options))

    def get_spin_speed_options(self) -> list[str]:
        return [str(v) for v in self.config.get_values('spin_speed') if v != "unknown"]

    def get_select_option(self, name: str, value):
        value = self._clean_value(value)
        if value is None:
            return None
        if name == "program":
            return self._display_program_value(value)
        if name == "temperature":
            if str(value).lower() in ("cold", "0"):
                return "Холодная"
            number = self._as_number(value)
            return str(number) if number is not None else str(value)
        if name == "spin_speed":
            number = self._as_number(value)
            return str(number) if number is not None else str(value)
        return str(value)

    def set_program(self, value: str) -> None:
        config_value = self._program_display_to_config_value(value)
        display_value = self._display_program_value(config_value)
        if commands := self.get_commands("program", config_value):
            self._send_single_command(commands[0])
            self.program_code = str(config_value)
            self.program = display_value
            self._last_program_display = display_value
            if str(config_value) == "0" or str(display_value) in ("0", "Выключено", "Выключен", "Выключена", "off", "Off", "OFF"):
                # The old program select can still exist in HA until entities are reloaded.
                # Treat selecting Off there exactly like physical selector code 0.
                self.selected_program_code = "0"
                self.selected_program = "Выключено"
                self.program = "Выключено"
                self.status = "Выключено"
                self.program_summary = "Выключено"
                self.phase = "Нет"
                self._program_duration_locked = False
                self.program_remaining_time = 0
                self.remaining_time = 0
                self.power = 0

    def set_temperature(self, value: str) -> None:
        config_value = "cold" if str(value) == "Холодная" else value
        if commands := self.get_commands("temperature", config_value):
            self._send_single_command(commands[0])
            self.temperature = self._as_number(0 if config_value == "cold" else config_value)

    def set_spin_speed(self, value: str) -> None:
        if commands := self.get_commands("spin_speed", value):
            self._send_single_command(commands[0])
            self.spin_speed = value

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        # Program select is intentionally not exposed: changing programs through
        # this entity is unreliable on these washers, while sensors still show
        # the selected/current program.
        if self.config['temperature'] is not None:
            entities.append(select.HaierWMTemperatureSelect(self))
        if self.config['spin_speed'] is not None:
            entities.append(select.HaierWMSpinSpeedSelect(self))
        return entities

    def init_if_needed(self) -> None:
        if self._inited:
            return
        commands = self.config.get_command_by_name("get_all_property") or [{
            "commandName": "getAllProperty",
            "value": "getAllProperty",
        }]
        _LOGGER.debug("%s: WM init getAllProperty commands=%s", self.device_name, commands)
        for command in commands:
            self._send_single_command(command)
        self._inited = True


    def _wm_sensor_available(self, name: str) -> bool:
        """Return True when a WM sensor can be useful.

        Older code created WM entities only when the model YAML contained the
        exact named attribute. For unknown/partially detected models this meant
        that one missing mapping could hide otherwise valid values.  Now we
        create sensors per field when either a config mapping exists or the
        value was already discovered from the device data/autodetect fallback.
        """
        try:
            if self.config[name] is not None:
                return True
        except Exception:
            pass
        return getattr(self, name, None) not in (None, "None", "unknown")

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        # Create WM entities independently: if a model profile/autodetect found
        # one field, expose that field even when other mappings are missing.
        if self._wm_sensor_available('program_remaining_time') or self._wm_sensor_available('remaining_time'):
            entities.append(sensor.HaierWMProgramRemainingTimeSensor(self))
        if self._wm_sensor_available('i_time') or self._get_named_attr_by_code('51') is not None:
            entities.append(sensor.HaierWMITimeSensor(self))
        if self._wm_sensor_available('program_duration'):
            entities.append(sensor.HaierWMProgramDurationSensor(self))
        if self._wm_sensor_available('program_summary') or self._wm_sensor_available('selected_program'):
            entities.append(sensor.HaierWMProgramSummarySensor(self))
        if self._wm_sensor_available('status'):
            entities.append(sensor.HaierWMStatusSensor(self))
        if self._wm_sensor_available('selected_program'):
            entities.append(sensor.HaierWMSelectedProgramSensor(self))
        if self._wm_sensor_available('program'):
            entities.append(sensor.HaierWMProgramSensor(self))
        if self._wm_sensor_available('temperature'):
            entities.append(sensor.HaierWMTemperatureSensor(self))
        if self._wm_sensor_available('spin_speed'):
            entities.append(sensor.HaierWMSpinSpeedSensor(self))
        if self._wm_sensor_available('energy'):
            entities.append(sensor.HaierWMEnergySensor(self))
        if self._wm_sensor_available('power'):
            entities.append(sensor.HaierWMPowerSensor(self))
        if self._wm_sensor_available('water_raw'):
            entities.append(sensor.HaierWMWaterConsumptionSensor(self))
        if self._wm_sensor_available('program_progress'):
            entities.append(sensor.HaierWMProgramProgressSensor(self))
        if self._wm_sensor_available('drum_clean_wash_count') or self._wm_sensor_available('wash_count'):
            entities.append(sensor.HaierWMDrumCleanWashCountSensor(self))
        if self._wm_sensor_available('total_wash_count'):
            entities.append(sensor.HaierWMTotalWashCountSensor(self))
        if self._wm_sensor_available('rinse_count'):
            entities.append(sensor.HaierWMRinseCountSensor(self))
        if self._wm_sensor_available('dirt_level'):
            entities.append(sensor.HaierWMDirtLevelSensor(self))
        named_diag = {
            "raw_22": ("WM Диагностика: Код 22", "mdi:remote"),
            "raw_62": ("WM Диагностика: Код состояния 62", "mdi:information-outline"),
        }
        # Keep only unknown/experimental raw values in diagnostics.
        # Codes already promoted to normal or binary sensors are intentionally
        # not exposed as duplicate WM raw entities.
        for raw_attr, raw_title in (
            ("raw_34", "WM Диагностика: Код 34"),
            ("raw_61", "WM Диагностика: Код 61"),
            ("raw_68", "WM Диагностика: Код 68"),
            ("raw_88", "WM Диагностика: Код 88"),
            ("raw_89", "WM Диагностика: Код 89"),
            ("raw_91", "WM Диагностика: Уровень/интенсивность"),
            ("raw_94", "WM Диагностика: Код 94"),
            ("raw_95", "WM Диагностика: Код 95"),
            ("raw_117", "WM Диагностика: Код 117"),
            ("raw_195", "WM Диагностика: Код 195"),
            ("raw_205", "WM Диагностика: Кандидат отсрочки 0.5"),
        ):
            if self._wm_sensor_available(raw_attr):
                entities.append(sensor.HaierWMRawDiagnosticSensor(self, raw_attr, raw_title))
        for raw_attr, (raw_title, raw_icon) in named_diag.items():
            if self._wm_sensor_available(raw_attr):
                entities.append(sensor.HaierWMNamedDiagnosticSensor(self, raw_attr, raw_title, raw_icon))
        if self._wm_sensor_available('phase'):
            entities.append(sensor.HaierWMPhaseSensor(self))
        if self._wm_sensor_available('phase_code'):
            entities.append(sensor.HaierWMPhaseCodeSensor(self))
        _LOGGER.debug("%s: WM created %s sensor entities", self.device_name, len(entities))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = []
        if self._wm_sensor_available('steam_function'):
            entities.append(binary_sensor.HaierWMSteamBinarySensor(self))
        if self._wm_sensor_available('anti_crease'):
            entities.append(binary_sensor.HaierWMAntiCreaseBinarySensor(self))
        if self._wm_sensor_available('delayed_start_enabled'):
            entities.append(binary_sensor.HaierWMDelayedStartBinarySensor(self))
        if self._wm_sensor_available('sound_notification'):
            entities.append(binary_sensor.HaierWMSoundNotificationBinarySensor(self))
        if self._wm_sensor_available('remote_control'):
            entities.append(binary_sensor.HaierWMRemoteControlBinarySensor(self))
        if self._wm_sensor_available('standby_mode'):
            entities.append(binary_sensor.HaierWMStandbyModeBinarySensor(self))
        if self._wm_sensor_available('child_lock'):
            entities.append(binary_sensor.HaierWMChildLockBinarySensor(self))
        if self._wm_sensor_available('door_lock'):
            entities.append(binary_sensor.HaierWMDoorLockBinarySensor(self))
        if self._wm_sensor_available('door_open') or self._wm_sensor_available('raw_31'):
            entities.append(binary_sensor.HaierWMDoorOpenBinarySensor(self))
        _LOGGER.debug("%s: WM created %s binary sensor entities", self.device_name, len(entities))
        return entities


def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False
