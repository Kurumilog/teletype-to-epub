#!/usr/bin/env python3
"""
Интерактивный парсер глав с teletype.in и сборка EPUB.
"""

import os
import re
import sys
import json
import time
import random
import hashlib
import base64
import requests
import traceback
import textwrap

from typing import List, Dict, Tuple, Optional, Set

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from ebooklib import epub
from PIL import Image
from io import BytesIO


# ─── Константы ───────────────────────────────────────────────────────────────

DEFAULT_LINKS_FILES = ["example.txt", "links.txt"]
CACHE_DIR = "cache"
IMAGES_DIR = "images"
DEFAULT_DELAY_MIN = 3
DEFAULT_DELAY_MAX = 7

CSS_CONTENT = """
body {
    font-family: serif;
    line-height: 1.6;
    margin: 1em;
}
h1 {
    font-size: 1.4em;
    margin-bottom: 1em;
    text-align: center;
}
h2, h3 {
    text-align: center;
    margin: 0.8em 0;
}
p {
    text-indent: 0;
    margin: 0.5em 0;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}
blockquote {
    margin: 1em 2em;
    font-style: italic;
}
"""


# ─── Классы и структуры ──────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self.book_title: str = ""
        self.book_author: str = ""
        self.cover_path: Optional[str] = None
        self.links_file: str = ""
        self.start_chapter: int = 0
        self.end_chapter: int = 0
        self.include_images: bool = True
        self.editor_priority: List[str] = []
        self.output_filename: str = ""
        
    @property
    def book_language(self) -> str:
        return "ru"


# ─── Selenium: инициализация ─────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    opts = Options()
    # Комментируем headless, чтобы видеть процесс (как в исходном коде)
    # opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    
    # Универсальный User-Agent (выглядит как обычный Windows Chrome, чтобы меньше блокировали)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Selenium 4.6+ умеет сам находить драйвер.
    # Если драйвер в PATH, он его найдет. Если нет — Selenium Manager попытается его скачать.
    service = Service() 

    try:
        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(60)
        return driver
    except Exception as e:
        print("\n❌ Ошибка запуска ChromeDriver.")
        print("Убедитесь, что Google Chrome установлен.")
        print(f"Детали ошибки: {e}")
        sys.exit(1)


# ─── Парсинг ссылок ──────────────────────────────────────────────────────────

def parse_links_file(filepath: str) -> Tuple[Dict[int, Dict[str, str]], List[str]]:
    """
    Парсит файл со ссылками.
    
    Возвращает:
        chapters: dict[номер_главы, dict[редактор, url]]
        all_editors: список всех найденных редакторов (никнеймов teletype)
    """
    
    # Структура: { 310: { '@cult': 'url...', '@grape': 'url...' } }
    chapters: Dict[int, Dict[str, str]] = {}
    editors_set: Set[str] = set()

    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    # Регулярка ищет: "Глава 123 (https://teletype.in/@username/slug...)"
    # Группа 1: номер главы
    # Группа 2: ссылка целиком
    # Группа 3: никнейм автора ссыки (включая @)
    pattern = re.compile(
        r"[Гг]лава\s+(\d+).*?\(?(https?://teletype\.in/(@[\w\-_]+)/[^\s\)\n\?]+)", 
        re.MULTILINE | re.IGNORECASE
    )

    for m in pattern.finditer(text):
        num = int(m.group(1))
        url = m.group(2).strip().rstrip(")")
        editor = m.group(3)

        if num not in chapters:
            chapters[num] = {}
        
        chapters[num][editor] = url
        editors_set.add(editor)

    return chapters, sorted(list(editors_set))


# ─── Парсинг одной главы ─────────────────────────────────────────────────────

def fetch_chapter(driver: webdriver.Chrome, url: str, include_images: bool) -> dict:
    """Возвращает {'title': str, 'html': str, 'images': [(url, bytes), ...]}"""

    driver.get(url)

    # Ждём загрузки
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "article.article__content"))
    )
    time.sleep(1.5) # Небольшая пауза для верности

    # ── Заголовок ──
    try:
        title_el = driver.find_element(By.CSS_SELECTOR, "h1.article__header_title")
        title = title_el.text.strip()
    except Exception:
        title = ""

    # ── Контент ──
    article = driver.find_element(By.CSS_SELECTOR, "article.article__content")
    children = article.find_elements(By.XPATH, "./*")

    content_parts: list[str] = []
    images: list[tuple[str, bytes]] = []

    for child in children:
        tag = child.tag_name.lower()

        if tag == "figure":
            if not include_images:
                continue # Пропускаем картинки
                
            try:
                img_el = child.find_element(By.TAG_NAME, "img")
                img_url = img_el.get_attribute("src")
                if img_url:
                    img_data = download_image(img_url)
                    if img_data:
                        img_hash = hashlib.md5(img_url.encode()).hexdigest()
                        ext = "jpg" if "jpeg" in img_url or "jpg" in img_url else "png"
                        img_filename = f"img_{img_hash}.{ext}"
                        images.append((img_filename, img_data))
                        content_parts.append(
                            f'<p style="text-align:center;">'
                            f'<img src="images/{img_filename}" alt="" />'
                            f"</p>"
                        )
            except Exception:
                pass
            continue

        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
            inner = get_inner_html(driver, child)
            inner = clean_html(inner)
            if inner.strip():
                # Проверяем центрирование
                align = child.get_attribute("data-align") or ""
                style = ' style="text-align:center;"' if align == "center" else ""
                content_parts.append(f"<{tag}{style}>{inner}</{tag}>")

    html = "\n".join(content_parts)
    return {"title": title, "html": html, "images": images}


def get_inner_html(driver: webdriver.Chrome, element) -> str:
    return driver.execute_script("return arguments[0].innerHTML;", element)


def clean_html(html: str) -> str:
    # Очистка от мусора teletype
    html = re.sub(r'<a\s+name="[^"]*"\s*>\s*</a\s*>', "", html)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r'\s+data-[\w-]+="[^"]*"', "", html)
    html = re.sub(r"\s{2,}", " ", html).strip()
    return html


def download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"  ⚠ Image fail: {e}")
        return None


# ─── Кэширование ─────────────────────────────────────────────────────────────

def get_cache_filename(chapter_num: int, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"chapter_{chapter_num}.json")

def save_cache(chapter_data: dict, cache_dir: str):
    os.makedirs(cache_dir, exist_ok=True)
    path = get_cache_filename(chapter_data["chapter_num"], cache_dir)
    
    # Сериализация для JSON
    to_save = {
        "chapter_num": chapter_data["chapter_num"],
        "title": chapter_data["title"],
        "html": chapter_data["html"],
        "images": [
            {"filename": fname, "data_b64": base64.b64encode(data).decode('ascii')}
            for fname, data in chapter_data.get("images", [])
        ],
        "has_images": bool(chapter_data.get("images"))
    }
    
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False)

def load_cache(chapter_num: int, cache_dir: str) -> dict | None:
    path = get_cache_filename(chapter_num, cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # Восстановление байтов картинок
        data["images"] = [
            (img["filename"], base64.b64decode(img["data_b64"]))
            for img in data.get("images", [])
        ]
        return data
    except Exception:
        return None


# ─── Сборка EPUB ─────────────────────────────────────────────────────────────

def build_epub_file(chapters_data: list[dict], config: Config):
    book = epub.EpubBook()

    # Метаданные
    book.set_identifier(f"teletype-builder-{int(time.time())}")
    book.set_title(config.book_title)
    book.set_language(config.book_language)
    book.add_author(config.book_author)

    # Обложка
    if config.cover_path and os.path.exists(config.cover_path):
        try:
            with open(config.cover_path, "rb") as f:
                book.set_cover("cover.jpg", f.read())
            print("✓ Обложка добавлена")
        except Exception as e:
            print(f"⚠ Ошибка добавления обложки: {e}")

    # CSS
    style = epub.EpubItem(
        uid="style", file_name="style/default.css",
        media_type="text/css", content=CSS_CONTENT.encode("utf-8")
    )
    book.add_item(style)

    spine = ["nav"]
    toc = []
    added_images = set()

    # Картинки
    for ch_data in chapters_data:
        for img_filename, img_bytes in ch_data.get("images", []):
            if img_filename not in added_images:
                ext = "png" if img_filename.endswith(".png") else "jpeg"
                img_item = epub.EpubItem(
                    uid=img_filename.replace(".", "_"),
                    file_name=f"images/{img_filename}",
                    media_type=f"image/{ext}",
                    content=img_bytes
                )
                book.add_item(img_item)
                added_images.add(img_filename)

    # Главы
    for ch_data in chapters_data:
        num = ch_data["chapter_num"]
        title = ch_data.get("title") or f"Глава {num}"
        
        # Если заголовок пустой или странный, используем номер
        if not title.strip():
            title = f"Глава {num}"

        ch_item = epub.EpubHtml(
            title=title,
            file_name=f"chapter_{num}.xhtml",
            lang=config.book_language
        )
        ch_item.content = f"<h1>{title}</h1>{ch_data['html']}".encode("utf-8")
        ch_item.add_item(style)

        book.add_item(ch_item)
        spine.append(ch_item)
        toc.append(epub.Link(f"chapter_{num}.xhtml", title, f"ch{num}"))

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    epub.write_epub(config.output_filename, book, {})
    print(f"\n✅ EPUB создан: {config.output_filename}")


# ─── Интерактивное меню ──────────────────────────────────────────────────────

def clear_screen():
    print("\033[H\033[J", end="")

def user_input(prompt: str, default: str = "") -> str:
    if default:
        res = input(f"{prompt} [{default}]: ").strip()
        return res if res else default
    return input(f"{prompt}: ").strip()

def setup_config() -> Config:
    conf = Config()
    clear_screen()
    print("═" * 50)
    print("   Teletype EPUB Builder (Interactive)")
    print("═" * 50)
    print()

    # 1. Название и автор книги
    conf.book_title = user_input("1. Название книги", "My Web Novel")
    conf.book_author = user_input("2. Автор книги", "Unknown Author")
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "", conf.book_title).replace(" ", "_")
    conf.output_filename = f"{safe_title}.epub"

    # 2. Поиск файла ссылок
    available_files = [f for f in DEFAULT_LINKS_FILES if os.path.exists(f)]
    default_file = available_files[0] if available_files else ""
    
    while True:
        fpath = user_input("3. Файл со ссылками (txt)", default_file)
        if os.path.exists(fpath):
            conf.links_file = fpath
            break
        print("   ❌ Файл не найден, попробуйте еще раз.")

    # Парсим файл, чтобы узнать диапазон и редакторов
    print(f"   ...анализ {conf.links_file}...")
    chapters_map, all_editors = parse_links_file(conf.links_file)
    
    if not chapters_map:
        print("   ❌ В файле не найдено ссылок teletype.in!")
        sys.exit(1)
        
    min_ch = min(chapters_map.keys())
    max_ch = max(chapters_map.keys())
    print(f"   ✓ Найдено глав: {len(chapters_map)} (от {min_ch} до {max_ch})")
    print(f"   ✓ Найдено источников: {', '.join(all_editors)}")
    print()

    # 3. Приоритетность редакторов
    print("4. Приоритет источников (укажите номера через запятую)")
    for idx, ed in enumerate(all_editors, 1):
        count = sum(1 for ch in chapters_map.values() if ed in ch)
        print(f"   {idx}. {ed} ({count} глав)")
    
    while True:
        prio_str = user_input("   Ваш выбор (например '1, 2')")
        try:
            choices = [int(x.strip()) for x in prio_str.split(",") if x.strip().isdigit()]
            selected_editors = []
            for c in choices:
                if 1 <= c <= len(all_editors):
                    ed = all_editors[c-1]
                    if ed not in selected_editors:
                        selected_editors.append(ed)
            
            # Добавляем оставшихся в конец автоматически
            for ed in all_editors:
                if ed not in selected_editors:
                    selected_editors.append(ed)
            
            conf.editor_priority = selected_editors
            print(f"   > Порядок: {' -> '.join(conf.editor_priority)}")
            break
        except ValueError:
            print("   Некорректный ввод.")

    # 4. Диапазон глав
    print()
    while True:
        try:
            s = user_input("5. Начальная глава", str(min_ch))
            e = user_input("   Конечная глава", str(max_ch))
            conf.start_chapter = int(s)
            conf.end_chapter = int(e)
            if conf.start_chapter > conf.end_chapter:
                print("   ❌ Начало больше конца!")
                continue
            break
        except ValueError:
            print("   Введите числа.")

    # 5. Картинки
    ans = user_input("6. Скачивать картинки? (y/n)", "y").lower()
    conf.include_images = (ans == 'y' or ans == 'yes')

    # 6. Обложка
    print()
    cov = user_input("7. Путь к обложке (Enter - без обложки)")
    if cov:
        # Убираем кавычки если пользователь скопировал путь как "path"
        cov = cov.strip('"').strip("'")
        if os.path.exists(cov):
            conf.cover_path = cov
        else:
            print(f"   ⚠ Файл не найден: {cov}. Будет создан EPUB без обложки.")
    
    return conf, chapters_map


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    try:
        conf, chapters_map = setup_config()
    except KeyboardInterrupt:
        print("\n\nОтмена.")
        return

    # Валидация: проверяем, что для каждой выбранной главы есть хотя бы одна ссылка из приоритетных
    missing_chapters = []
    chapters_queue = [] # [(num, url), ...]

    print("\n🔍 Проверка доступности глав...")
    
    for num in range(conf.start_chapter, conf.end_chapter + 1):
        if num not in chapters_map:
            missing_chapters.append(num)
            continue
        
        # Ищем URL по приоритету
        found_url = None
        for editor in conf.editor_priority:
            if editor in chapters_map[num]:
                found_url = chapters_map[num][editor]
                break
        
        if found_url:
            chapters_queue.append((num, found_url))
        else:
            missing_chapters.append(num)

    if missing_chapters:
        print(f"\n❌ ОШИБКА: Для следующих глав нет ссылок у выбранных авторов:\n{missing_chapters}")
        print("Пожалуйста, расширьте диапазон авторов или измените выбор глав.")
        sys.exit(1)

    print(f"   ✓ Всё готово к парсингу {len(chapters_queue)} глав.")
    print(f"   Файл: {conf.output_filename}")
    input("\nНажмите Enter для старта...")

    # Инициализация
    os.makedirs(CACHE_DIR, exist_ok=True)
    if conf.include_images:
        os.makedirs(IMAGES_DIR, exist_ok=True)

    result_data = []
    driver = None

    try:
        # Сначала проверяем кэш
        uncached_queue = []
        for num, url in chapters_queue:
            cached = load_cache(num, CACHE_DIR)
            # Если в кэше есть, и мы хотим картинки, но в кэше их нет (или наоборот) -> лучше перекачать?
            # Упростим: если есть в кэше, берем из кэша.
            # Но если пользователь хочет картинки, а в кэше has_images=False, то надо бы перекачать. 
            # Добавим простую проверку:
            
            need_reparse = False
            if cached:
                # Если мы хотим картинки, а в кэше их нет (и флаг has_images=False)
                if conf.include_images and not cached.get("has_images", False) and not cached.get("images"):
                     # Возможно глава просто без картинок, но мы не знаем.
                     # Для простоты: верим кэшу.
                     pass

            if cached:
                print(f"📖 Глава {num} взята из кэша.")
                result_data.append(cached)
            else:
                uncached_queue.append((num, url))

        # Если что-то осталось не из кэша
        if uncached_queue:
            print("\n🌐 Запуск браузера...")
            driver = make_driver()
            
            total = len(uncached_queue)
            for idx, (num, url) in enumerate(uncached_queue, 1):
                print(f"[{idx}/{total}] Парсинг главы {num}...")
                print(f"   Url: {url}")
                
                # Попытка парсинга с ретраями (на случай сетевых сбоев)
                retries = 3
                success = False
                while retries > 0:
                    try:
                        data = fetch_chapter(driver, url, conf.include_images)
                        data["chapter_num"] = num
                        result_data.append(data)
                        save_cache(data, CACHE_DIR)
                        
                        sz = len(data['html'])
                        imgs = len(data['images'])
                        print(f"   ✓ OK. Текст: {sz}, Изображений: {imgs}")
                        success = True
                        break
                    except Exception as e:
                        print(f"   ⚠ Ошибка: {e}")
                        retries -= 1
                        time.sleep(2)
                
                if not success:
                    print(f"   ❌ Не удалось скачать главу {num} после всех попыток.")
                    # Пытаемся взять следующий приоритет?
                    # В текущей логике мы уже выбрали URL. 
                    # Можно усложнить и пробовать другой URL тут.
                    # Для текущей задачи - падаем или пропускаем.
                    # По заданию "ссылки нет -> exception". Тут ссылка есть, но не грузит.
                    raise Exception(f"Не удалось загрузить главу {num} по ссылке {url}")

                if idx < total:
                    time.sleep(random.uniform(DEFAULT_DELAY_MIN, DEFAULT_DELAY_MAX))

    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if driver:
            driver.quit()
            print("🌐 Браузер закрыт.")

    # Сортировка и билд
    result_data.sort(key=lambda x: x["chapter_num"])
    
    if result_data:
        print("\n📚 Генерация книги...")
        build_epub_file(result_data, config=conf)
    else:
        print("Нет данных для сборки.")

if __name__ == "__main__":
    main()
