import requests
import tomllib
from pathlib import Path
import socket

cfg = {}
if Path("config.toml").exists():
    with open("config.toml","rb") as f:
        try:
            cfg = tomllib.load(f)
        except Exception as e:
            print("Не удалось прочитать config.toml:", e)

proxy = cfg.get("proxy_string")
change = cfg.get("proxy_change_url")

if not proxy:
    print("proxy_string не найден в config.toml")
    raise SystemExit(1)

def mask(proxy_str: str) -> str:
    return proxy_str.split('@')[0] + "@***" if '@' in proxy_str else proxy_str

print("Configured proxy:", mask(proxy))

tests = []

# 1) Try as provided (http/https)
tests.append(("as_config", {"http": proxy, "https": proxy}))

# 2) Try socks5:// variant (if not already socks)
if not proxy.startswith("socks"):
    socks_proxy = proxy.replace("http://", "socks5://") if proxy.startswith("http://") else "socks5://" + proxy
    tests.append(("socks5_try", {"http": socks_proxy, "https": socks_proxy}))

# 3) Direct (no proxy)
tests.append(("direct", None))

for name, proxies in tests:
    print(f"\n---- Testing: {name} ----")
    # test ipify
    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
        print("ipify status:", r.status_code, r.text)
    except Exception as e:
        print("ipify check failed:", e)

    # test vinted
    try:
        r = requests.get("https://www.vinted.de", proxies=proxies, timeout=15)
        print("vinted.de status:", r.status_code)
        print("headers sample:", dict(list(r.headers.items())[:5]))
    except Exception as e:
        print("vinted.de request failed:", e)

    # quick TCP connect to proxy host:port (for non-direct tests)
    if proxies:
        try:
            # extract host:port naive
            hostport = proxy.split('@')[-1] if '@' in proxy else proxy
            host, port = hostport.split(":")[:2]
            port = int(port)
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            print(f"TCP connect to {host}:{port} OK")
        except Exception as e:
            print(f"TCP connect failed: {e}")

print("\nAll tests done.")
