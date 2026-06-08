import requests
import logging
import time
from typing import Optional

log = logging.getLogger("vinted-bot")


def get_current_ip(proxy_string: str = None) -> Optional[str]:
    """Получает текущий внешний IP через прокси"""
    try:
        proxies = None
        if proxy_string:
            proxies = {"http": proxy_string, "https": proxy_string}

        response = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
        )

        if response.status_code == 200:
            ip = response.json().get("ip")
            log.debug(f"Current IP: {ip}")
            return ip
    except Exception as e:
        log.warning(f"Failed to get current IP: {e}")
    return None


class Proxy:
    """Интерфейс прокси"""

    def get_proxy_string(self) -> Optional[str]:
        raise NotImplementedError

    def is_valid(self) -> bool:
        raise NotImplementedError

    def change_ip(self) -> bool:
        return False

    def handle_block(self, status_code: int = 429):
        return False


class NoProxy(Proxy):
    """Пустой прокси (без прокси)"""

    def get_proxy_string(self) -> Optional[str]:
        return None

    def is_valid(self) -> bool:
        return False


class ServerProxy(Proxy):
    """Обычный серверный прокси"""

    def __init__(self, proxy_string: str):
        self.proxy_string = proxy_string
        self._current_proxy = proxy_string

    def get_proxy_string(self) -> Optional[str]:
        return self._current_proxy

    def change_ip(self) -> bool:
        log.warning("ServerProxy: cannot change IP (static proxy)")
        return False

    def handle_block(self, status_code: int = 429):
        log.warning(f"ServerProxy: received {status_code} but cannot change IP")
        return False

    def is_valid(self) -> bool:
        return bool(self._current_proxy)


class MobileProxy(ServerProxy):
    """Мобильный прокси с поддержкой смены IP"""

    def __init__(self, proxy_string: str, change_url: str):
        super().__init__(proxy_string)
        self.proxy_change_url = change_url
        self._last_change = 0
        self._change_count = 0
        self._min_change_interval = 120  # минимум 2 минуты между сменами
        self._last_ip = None

    def _check_ip_changed(self) -> bool:
        # пытаемся не один раз убедиться, что IP поменялся
        attempts = 3
        for _ in range(attempts):
            new_ip = get_current_ip(self._current_proxy)
            if new_ip:
                if self._last_ip and new_ip == self._last_ip:
                    log.warning(f"⚠️ IP не изменился: {new_ip}")
                    return False
                else:
                    self._last_ip = new_ip
                    return True
        return False

    def change_ip(self) -> bool:
        if not self.proxy_change_url:
            return False
        now = time.time()
        if now - self._last_change < self._min_change_interval:
            log.warning("MobileProxy: too soon to change IP again")
            return False
        try:
            resp = requests.get(self.proxy_change_url, timeout=10)
            if resp.status_code == 200:
                self._last_change = now
                self._change_count += 1
                # кратковременная пауза, чтобы провайдер успел сменить IP
                time.sleep(1)
                return self._check_ip_changed()
        except Exception as e:
            log.warning(f"MobileProxy change_ip failed: {e}")
        return False

    def handle_block(self, status_code: int = 429):
        # при блокировке пробуем сменить IP
        return self.change_ip()
