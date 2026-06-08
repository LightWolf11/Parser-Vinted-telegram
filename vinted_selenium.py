
import logging
import json
import time
from datetime import datetime
from typing import List, Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

log = logging.getLogger("vinted-selenium")

class VintedItem:
    """Простой класс для товара."""
    def __init__(self, data: dict):
        self.id = data.get("id")
        self.title = data.get("title", "")
        self.url = data.get("url", "")
        self.brand_title = data.get("brand_title", "")
        self.price = data.get("price", {})
        self.photo = type('Photo', (), {'url': data.get("photo_url", "")})()
        self.created_at = data.get("created_at", "")
        self.user = type('User', (), {'login': data.get("user_login", "")})()

class VintedSelenium:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = None
        self.wait = None
        self._init_driver()

    def _init_driver(self):
        """Создаём и конфигурируем Chrome driver."""
        try:
            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
            
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
            self.wait = WebDriverWait(self.driver, 10)
            log.info("Selenium Chrome driver initialized")
        except Exception as e:
            log.error("Failed to initialize Selenium driver: %s", e)
            raise

    def search(self, brand_id: Optional[int] = None, brand_name: Optional[str] = None, per_page: int = 96, order: str = "newest_first") -> List[VintedItem]:
        """
        Ищет товары по бренду ID или названию.
        Загружает страницу через браузер и парсит JSON из скрипта.
        """
        try:
            # Формируем URL для поиска
            base_url = "https://www.vinted.com/api/v2/catalog/items"
            params = f"?page=1&per_page={per_page}&order={order}"
            
            if brand_id:
                url = f"https://www.vinted.com/catalog?brand_ids={brand_id}&order={order}"
            else:
                url = f"https://www.vinted.com/catalog?search_text={brand_name}&order={order}"
            
            log.info("Loading URL: %s", url)
            self.driver.get(url)
            
            # Ждём загрузки товаров (ищем сетку товаров)
            time.sleep(2)
            
            # Пытаемся найти JSON с товарами в скрипте страницы
            items = self._extract_items_from_page()
            log.info("Extracted %s items from page", len(items))
            return items
            
        except Exception as e:
            log.error("Search error: %s", e)
            return []

    def _extract_items_from_page(self) -> List[VintedItem]:
        """Парсит товары со страницы через JS/JSON в разметке."""
        try:
            # Находим все элементы товаров (data-testid="catalogGrid-item")
            items_data = []
            
            # Способ 1: Ищем скрипт с JSON (Vinted использует SSR с данными в скрипте)
            scripts = self.driver.find_elements(By.TAG_NAME, "script")
            for script in scripts:
                content = script.get_attribute("textContent")
                if content and "catalogue" in content.lower():
                    try:
                        # Пытаемся распарсить JSON из скрипта
                        json_start = content.find("{")
                        json_end = content.rfind("}") + 1
                        if json_start >= 0 and json_end > json_start:
                            json_str = content[json_start:json_end]
                            data = json.loads(json_str)
                            if "catalogue" in data and "items" in data["catalogue"]:
                                for item in data["catalogue"]["items"]:
                                    items_data.append(VintedItem({
                                        "id": item.get("id"),
                                        "title": item.get("title"),
                                        "url": f"https://www.vinted.com/items/{item.get('id')}",
                                        "brand_title": item.get("brand", {}).get("title", ""),
                                        "price": item.get("price", {}),
                                        "photo_url": item.get("photo", {}).get("url", ""),
                                        "created_at": item.get("created_at", ""),
                                        "user_login": item.get("user", {}).get("login", ""),
                                    }))
                    except json.JSONDecodeError:
                        pass
            
            # Если не нашли через JSON, парсим DOM напрямую
            if not items_data:
                items_data = self._extract_items_from_dom()
            
            return items_data
        except Exception as e:
            log.error("Failed to extract items from page: %s", e)
            return []

    def _extract_items_from_dom(self) -> List[VintedItem]:
        """Парсит товары прямо из DOM элементов."""
        try:
            items_data = []
            # Ищем карточки товаров
            item_elements = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='catalogGrid-item']")
            
            for elem in item_elements:
                try:
                    title_elem = elem.find_element(By.CSS_SELECTOR, "h2, [class*='title']")
                    title = title_elem.text if title_elem else "Unknown"
                    
                    link_elem = elem.find_element(By.TAG_NAME, "a")
                    url = link_elem.get_attribute("href") if link_elem else ""
                    
                    # Извлекаем ID из URL
                    item_id = url.split("/")[-1] if url else None
                    
                    items_data.append(VintedItem({
                        "id": int(item_id) if item_id and item_id.isdigit() else None,
                        "title": title,
                        "url": url,
                        "brand_title": "",
                        "price": {},
                        "photo_url": "",
                        "created_at": "",
                        "user_login": "",
                    }))
                except Exception as item_e:
                    log.debug("Failed to parse item: %s", item_e)
                    continue
            
            return items_data
        except Exception as e:
            log.error("Failed to extract items from DOM: %s", e)
            return []

    def close(self):
        """Закрываем браузер."""
        if self.driver:
            self.driver.quit()
            log.info("Selenium driver closed")

    def __del__(self):
        self.close()
