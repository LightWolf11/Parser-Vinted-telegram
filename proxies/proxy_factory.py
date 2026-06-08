from .proxy import NoProxy, ServerProxy, MobileProxy, Proxy
import os
import logging

log = logging.getLogger("vinted-bot")


def build_proxy(proxy_str: str | None = None, change_url: str | None = None) -> Proxy:
    """Определяет тип прокси.

    Аргументы позволяют передать значения из внешнего конфига (toml или env).
    Если параметры не переданы, берём из переменных окружения.
    """
    if proxy_str is None:
        proxy_str = os.getenv("VINTED_PROXY")
    if change_url is None:
        change_url = os.getenv("VINTED_PROXY_CHANGE_URL")

    if change_url and not proxy_str:
        raise ValueError("VINTED_PROXY_CHANGE_URL задан без VINTED_PROXY")

    if proxy_str and change_url:
        log.info("Прокси определён как мобильный")
        return MobileProxy(proxy_str, change_url)

    if proxy_str:
        log.info("Прокси определён как серверный: %s",
                 proxy_str.split('@')[0] + '@***' if '@' in proxy_str else proxy_str)
        return ServerProxy(proxy_str)

    log.warning("Прокси не указан, будет использоваться прямое соединение")
    return NoProxy()
