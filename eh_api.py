# Copyright (C) 2025 OrPudding
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import re
import io
import sys
import math
import struct
import requests
import logging
from urllib.parse import urlencode, quote, unquote
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from PIL import Image
from cachetools import cached, TTLCache
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# 缓存配置
# ==============================================================================
list_cache = TTLCache(maxsize=100, ttl=300 )
gallery_cache = TTLCache(maxsize=500, ttl=3600)
image_proxy_cache = TTLCache(maxsize=1000, ttl=86400)
pagination_cache = TTLCache(maxsize=200, ttl=600)
tag_translation_cache = TTLCache(maxsize=32, ttl=86400)


def decode_search_value(value: str) -> str:
    """
    判断并解码搜索值
    如果值是URL编码，则解码为中文，否则直接返回
    """
    # URL编码的特征：包含%后跟两个十六进制字符
    url_encoded_pattern = r"%[0-9A-Fa-f]{2}"

    # 如果包含URL编码特征，尝试解码
    if re.search(url_encoded_pattern, value):
        try:
            decoded = unquote(value)
            # 解码后如果还包含URL编码特征，说明可能有多重编码，继续解码
            while re.search(url_encoded_pattern, decoded):
                temp = unquote(decoded)
                if temp == decoded:  # 如果没有变化，停止解码
                    break
                decoded = temp
            return decoded
        except Exception:
            # 如果解码失败，返回原值
            return value
    else:
        # 没有URL编码特征，直接返回
        return value


TAG_TRANSLATION_BASE_URL = "https://raw.githubusercontent.com/EhTagTranslation/Database/master/database"
TAG_NAMESPACE_FILES = {
    "artist": "artist.md",
    "character": "character.md",
    "cosplayer": "cosplayer.md",
    "female": "female.md",
    "group": "group.md",
    "language": "language.md",
    "location": "location.md",
    "male": "male.md",
    "mixed": "mixed.md",
    "misc": "mixed.md",
    "other": "other.md",
    "parody": "parody.md",
    "reclass": "reclass.md",
}


def is_chinese_locale(device_info: dict) -> bool:
    language = (device_info.get('language') or '').lower()
    region = (device_info.get('region') or '').upper()
    return language.startswith('zh') or region in ('CN', 'TW', 'HK', 'MO')


def clean_tag_translation_text(text: str) -> str:
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('`', '')
    text = re.sub(r'[\U0001F000-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]', '', text)
    return text.strip()


def parse_tag_translation_markdown(text: str) -> Dict[str, str]:
    translations = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('|') or line.startswith('| ---') or '原始标签' in line:
            continue
        columns = [column.strip() for column in line.strip('|').split('|')]
        if len(columns) < 2:
            continue
        raw_tag, translated_name = columns[0].strip('` '), clean_tag_translation_text(columns[1])
        if raw_tag and translated_name and not translated_name.startswith('=='):
            translations[raw_tag.lower()] = translated_name
    return translations


@cached(cache=tag_translation_cache)
def get_tag_translation_map(namespace: str) -> Dict[str, str]:
    filename = TAG_NAMESPACE_FILES.get(namespace.lower())
    if not filename:
        return {}
    url = f"{TAG_TRANSLATION_BASE_URL}/{filename}"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return parse_tag_translation_markdown(response.text)
    except Exception as e:
        logging.warning(f"加载 EhTagTranslation 数据失败: {namespace}, {e}")
        return {}


def translate_eh_tag(namespace: str, tag: str) -> str:
    tag_text = tag.strip()
    if not tag_text:
        return tag
    translations = get_tag_translation_map(namespace.lower())
    return translations.get(tag_text.lower(), tag_text)


def translate_eh_category(category: str) -> str:
    if not category:
        return category
    return translate_eh_tag('reclass', category.replace(' ', '').lower()) or category


def flatten_eh_tags(detail: dict, translate: bool = False) -> List[str]:
    result = []
    category = detail.get('category')
    if category:
        result.append(translate_eh_category(category) if translate else category)

    tags = detail.get('tags')
    if isinstance(tags, dict):
        for tag_type, tag_list in tags.items():
            namespace = tag_type.lower().strip()
            if isinstance(tag_list, list):
                for tag in tag_list:
                    result.append(translate_eh_tag(namespace, tag) if translate else tag)
    return result

# ==============================================================================
# 模块 1: E-Hentai HTML 解析器 (EhParser)
# ==============================================================================
class EhParser:
    PATTERN_GALLERY_URL = re.compile(r'/g/(\d+)/([a-f0-9]+)/?')
    PATTERN_RATING = re.compile(r'background-position:\s*(-?\d+)px')
    PATTERN_PAGES = re.compile(r'(\d+)\s+pages?', re.IGNORECASE)
    PATTERN_NEXT_ID = re.compile(r'[?&]next=(\d+)')
    PATTERN_STYLE_DETAILS = re.compile(r'width:(\d+)px;height:(\d+)px;.*background:.*?url\(([^)]+)\) (-?\d+)px (-?\d+)')

    @staticmethod
    def parse_gallery_list(html: str) -> Dict:
        soup = BeautifulSoup(html, 'html.parser')
        galleries = []
        main_table = soup.find('table', class_='itg gltc')
        # 如果找不到主表格，记录日志并返回空结果
        if not main_table:
            logging.warning("未能解析到画廊列表 (找不到 'itg gltc' 表格)。页面原始内容如下：")
            logging.debug(html)
            return {'galleries': [], 'pagination': {}}
        
        rows = main_table.find_all('tr')
        for row in rows:
            try:
                name_cell = row.find('td', class_='glname')
                if not name_cell: continue
                gallery = {}
                link_tag = name_cell.find('a')
                if not link_tag or 'href' not in link_tag.attrs: continue
                url = link_tag['href']
                match = EhParser.PATTERN_GALLERY_URL.search(url)
                if not match: continue
                gallery['gid'] = int(match.group(1)); gallery['token'] = match.group(2); gallery['url'] = url
                title_div = link_tag.find('div', class_='glink')
                gallery['title'] = title_div.text.strip() if title_div else 'N/A'
                thumb_cell = row.find('td', class_='gl2c')
                if thumb_cell:
                    img_tag = thumb_cell.find('img')
                    if img_tag: gallery['thumbnail'] = img_tag.get('data-src') or img_tag.get('src')
                    posted_div = thumb_cell.find('div', id=lambda x: x and x.startswith('posted_'))
                    if posted_div: gallery['posted'] = posted_div.text.strip()
                category_cell = row.find('td', class_='glcat')
                if category_cell: gallery['category'] = category_cell.text.strip()
                rating_elem = name_cell.find('div', class_='ir')
                if rating_elem:
                    style = rating_elem.get('style', ''); rating_match = EhParser.PATTERN_RATING.search(style)
                    if rating_match: gallery['rating'] = round(5 - (abs(int(rating_match.group(1))) / 16.0), 2)
                uploader_cell = row.find('td', class_='glhide')
                if uploader_cell:
                    uploader_elem = uploader_cell.find('a')
                    if uploader_elem: gallery['uploader'] = uploader_elem.get_text(strip=True)
                    pages_text_node = uploader_cell.find(string=re.compile(r'\d+\s+pages?'))
                    if pages_text_node:
                        pages_match = EhParser.PATTERN_PAGES.search(pages_text_node)
                        if pages_match: gallery['pages'] = int(pages_match.group(1))
                galleries.append(gallery)
            except Exception as e: logging.error(f"解析画廊项时发生错误: {e}"); continue
        
        # 如果循环后列表仍为空，可能页面有内容但所有行都解析失败
        if not galleries and len(rows) > 1: # len(rows) > 1 是为了排除只有表头的情况
            logging.warning("画廊列表解析结果为空，可能所有行都解析失败。页面原始内容如下：")
            logging.debug(html)

        pagination = {'has_next': False, 'next_id': None}
        try:
            pager = soup.find('div', class_='searchnav') or soup.find('table', class_='ptt')
            if pager:
                next_link = pager.find('a', id='unext') or pager.find('a', text='>')
                if next_link and next_link.has_attr('href'):
                    pagination['has_next'] = True
                    href = next_link['href']
                    next_id_match = EhParser.PATTERN_NEXT_ID.search(href)
                    if next_id_match: pagination['next_id'] = next_id_match.group(1)
        except Exception as e: logging.error(f"解析分页信息时出错: {e}")
        return {'galleries': galleries, 'pagination': pagination}

    @staticmethod
    def parse_gallery_detail(html: str) -> Dict:
        soup = BeautifulSoup(html, 'html.parser'); detail = {}
        try:
            # 检查核心元素是否存在
            if not soup.select_one('#gn') and not soup.select_one('#gj'):
                logging.warning("未能解析到画廊详情 (找不到标题元素 #gn 或 #gj)。页面原始内容如下：")
                logging.debug(html)
                return {}

            title_elem = soup.select_one('#gn');
            if title_elem: detail['title'] = title_elem.get_text(strip=True)
            title_jp_elem = soup.select_one('#gj');
            if title_jp_elem: detail['title_jp'] = title_jp_elem.get_text(strip=True)
            category_elem = soup.select_one('#gdc a');
            if category_elem: detail['category'] = category_elem.get_text(strip=True)
            thumb_elem = soup.select_one('#gd1 div')
            if thumb_elem:
                style = thumb_elem.get('style', ''); url_match = re.search(r'url\((.+?)\)', style)
                if url_match: detail['thumbnail'] = url_match.group(1)
            tags = {}
            tag_list = soup.select('#taglist tr')
            for tag_row in tag_list:
                tag_type = tag_row.select_one('td.tc')
                if tag_type:
                    tag_name = tag_type.get_text(strip=True).rstrip(':'); tag_values = [tag_elem.get_text(strip=True) for tag_elem in tag_row.select('td div a')]
                    if tag_values: tags[tag_name] = tag_values
            if tags: detail['tags'] = tags
            rating_elem = soup.select_one('#rating_label')
            if rating_elem:
                rating_text = rating_elem.get_text(strip=True); rating_match = re.search(r'([\d.]+)', rating_text)
                if rating_match: detail['rating'] = float(rating_match.group(1))
            
            gdd_rows = soup.select('#gdd tr')
            for row in gdd_rows:
                label_elem = row.select_one('td.gdt1')
                value_elem = row.select_one('td.gdt2')
                if label_elem and value_elem:
                    label_text = label_elem.get_text(strip=True)
                    if 'Length:' in label_text or 'length:' in label_text.lower():
                        pages_text = value_elem.get_text(strip=True)
                        pages_match = re.search(r'(\d+)', pages_text)
                        if pages_match:
                            detail['pages'] = int(pages_match.group(1))
                            break

            comments = []
            for c1 in soup.select('#cdiv .c1'):
                c2 = c1.select_one('.c2')
                c3 = c1.select_one('.c3')
                if c2 and c3:
                    author_elem = c2.select_one('.c3 a, a')
                    author = author_elem.get_text(strip=True) if author_elem else "Unknown"
                    score_elem = c2.select_one('[id^="comment_score_"]')
                    score = score_elem.get_text(strip=True) if score_elem else "0"
                    content = c3.get_text(separator=' ', strip=True)
                    comments.append({"author": author, "score": score, "content": content})
            if comments:
                detail['comments'] = comments
        except Exception as e: logging.error(f"解析画廊详情时出错: {e}")
        
        # 如果最终字典为空，记录日志
        if not detail:
            logging.warning("画廊详情解析结果为空。页面原始内容如下：")
            logging.debug(html)

        return detail

    @staticmethod
    def get_archiver_download_url(html: str) -> Optional[str]:
        match = re.search(r'href="([^"]+)">Click Here To Start Downloading', html)
        return match.group(1) if match else None

    @staticmethod
    def parse_archiver_form_url(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, 'html.parser')
        form = soup.select_one('form[action*="archiver.php"]')
        return form.get('action') if form else None

    @staticmethod
    def parse_preview_images(html: str) -> List[Dict]:
        previews = []
        soup = BeautifulSoup(html, 'html.parser')
        container = soup.find('div', id='gdt')
        if not container:
            logging.warning("未能解析到预览图列表 (找不到容器 #gdt)。页面原始内容如下：")
            logging.debug(html)
            return previews
        
        image_links = container.find_all('a')
        for index, a_tag in enumerate(image_links):
            try:
                div_tag = a_tag.find('div')
                if not div_tag or 'style' not in div_tag.attrs: continue
                style = div_tag['style']
                details_match = EhParser.PATTERN_STYLE_DETAILS.search(style)
                if details_match:
                    width = int(details_match.group(1)); height = int(details_match.group(2))
                    thumbnail_url = details_match.group(3); x_offset = abs(int(details_match.group(4))); y_offset = abs(int(details_match.group(5)))
                    previews.append({'index': index, 'page_url': a_tag['href'], 'thumbnail_url': thumbnail_url, 'crop_x': x_offset, 'crop_y': y_offset, 'crop_w': width, 'crop_h': height})
            except Exception as e: logging.error(f"解析单个预览图时出错: {e}"); continue
        
        if not previews and image_links:
            logging.warning("预览图列表解析结果为空，但找到了 a 标签。页面原始内容如下：")
            logging.debug(html)

        return previews

    @staticmethod
    def parse_image_page(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, 'html.parser')
        img_container = soup.find('div', id='i3')
        if not img_container:
            logging.warning("未能解析到大图页面 (找不到容器 #i3)。页面原始内容如下：")
            logging.debug(html)
            return None
        img_tag = img_container.find('img')
        if not img_tag or 'src' not in img_tag.attrs:
            logging.warning("未能解析到大图 URL (在 #i3 中找不到带 src 的 img 标签)。页面原始内容如下：")
            logging.debug(html)
            return None
        return img_tag['src']


# ==============================================================================
# 模块 2: E-Hentai URL 构建器 (EhUrlBuilder)
# ==============================================================================
class EhUrlBuilder:
    SITE_E = 'e-hentai.org'; SITE_EX = 'exhentai.org'
    def __init__(self, use_exhentai: bool = False): self.domain = self.SITE_EX if use_exhentai else self.SITE_E; self.base_url = f'https://{self.domain}'
    def build_home_url(self, next_id: Optional[str] = None ) -> str: return f'{self.base_url}/' if not next_id else f'{self.base_url}/?next={next_id}'
    def build_favorites_url(self, favcat: str = 'all', page: int = 0) -> str:
        params = {}
        if favcat != 'all': params['favcat'] = favcat
        if page > 0: params['page'] = page
        qs = urlencode(params)
        return f'{self.base_url}/favorites.php?{qs}' if qs else f'{self.base_url}/favorites.php'
    def build_search_url(self, keyword: Optional[str] = None, next_id: Optional[str] = None, **kwargs) -> str:
        params = {}
        if not next_id:
            if keyword: params['f_search'] = keyword.strip()
        else:
            params = {'f_search': keyword.strip(), 'next': next_id} if keyword else {'next': next_id}
        query_string = urlencode(params)
        return f'{self.base_url}/?{query_string}'
    def build_tag_url(self, tag: str, page: int = 0) -> str: encoded_tag = quote(tag); return f'{self.base_url}/tag/{encoded_tag}' if page == 0 else f'{self.base_url}/tag/{encoded_tag}/{page}'
    def build_gallery_url(self, gid: int, token: str) -> str: return f'{self.base_url}/g/{gid}/{token}/'
    def build_popular_url(self) -> str: return f'{self.base_url}/popular'
    def get_referer(self) -> str: return self.base_url

# ==============================================================================
# 模块 3: 图片处理器 (ImageProcessor)
# ==============================================================================
class ImageProcessor:
    @staticmethod
    def is_truthy(value: str) -> bool:
        return value in ("1", "true", "True", "yes", "on")

    @staticmethod
    def normalize_rgb_image(img: Image.Image) -> Image.Image:
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode in ("RGBA", "LA"):
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            return background
        if img.mode != "RGB":
            return img.convert("RGB")
        return img

    @staticmethod
    def optimize_png_image(img: Image.Image, quality: int) -> Image.Image:
        quality = max(1, min(100, quality))
        img = ImageProcessor.normalize_rgb_image(img)
        if quality >= 95:
            return img
        colors = max(16, min(256, int(16 + quality * 2.4)))
        return img.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)

    @staticmethod
    def convert_to_lvgl8(img: Image.Image) -> bytes:
        img = ImageProcessor.normalize_rgb_image(img)
        img = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        w, h = img.size

        raw_palette = img.getpalette()
        palette = []
        for i in range(256):
            idx = i * 3
            if idx + 2 < len(raw_palette):
                palette.append((raw_palette[idx], raw_palette[idx + 1], raw_palette[idx + 2]))
            else:
                palette.append((0, 0, 0))

        header_word1 = 10 | (w << 10) | (h << 21)
        output = io.BytesIO()
        output.write(struct.pack("<I", header_word1))
        for r, g, b in palette:
            output.write(bytes([b, g, r, 0xFF]))
        output.write(img.tobytes())
        return output.getvalue()

    @staticmethod
    def process_and_compress(image_bytes: bytes, max_width: int, quality: int, crop_params: Optional[Dict] = None, output_format: str = "jpeg") -> Optional[Tuple[bytes, dict]]:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            if crop_params:
                x, y, w, h = crop_params['x'], crop_params['y'], crop_params['w'], crop_params['h']
                box = (x, y, x + w, y + h)
                img = img.crop(box)
            original_width, original_height = img.size
            if original_width > max_width:
                ratio = max_width / original_width; new_height = int(original_height * ratio)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if output_format == "lvgl":
                processed_bytes = ImageProcessor.convert_to_lvgl8(img)
                content_type = "application/octet-stream"
            elif output_format == "png":
                img = ImageProcessor.optimize_png_image(img, quality)
                img.save(output, "PNG", optimize=True, compress_level=9)
                processed_bytes = output.getvalue()
                content_type = "image/png"
            else:
                img = ImageProcessor.normalize_rgb_image(img)
                optimize_options = {"quality": quality, "optimize": True, "progressive": True}
                img.save(output, "JPEG", **optimize_options)
                processed_bytes = output.getvalue()
                content_type = "image/jpeg"
            info = {"original_size": f"{original_width}x{original_height}", "compressed_size": f"{img.width}x{img.height}", "file_size": len(processed_bytes), "content_type": content_type}
            return processed_bytes, info
        except Exception as e: logging.error(f"图片处理失败: {e}"); return None

# ==============================================================================
# 核心业务逻辑 (独立的、可缓存的函数)
# ==============================================================================
@cached(cache=list_cache)
def get_gallery_list_data(url: str, headers: tuple):
    logging.info(f"缓存未命中或已过期，正在抓取列表页: {url}")
    html = fetch_page_for_request(url, dict(headers))
    if not html:
        # fetch_page_for_request 内部已经记录了错误，这里无需重复记录
        return None
    
    parsed_data = EhParser.parse_gallery_list(html)
    # EhParser 内部已经增加了日志，这里返回即可
    return parsed_data

@cached(cache=gallery_cache)
def get_gallery_detail_data(gid: int, token: str, headers: tuple, url_builder: 'EhUrlBuilder'):
    url = url_builder.build_gallery_url(gid=gid, token=token)
    logging.info(f"缓存未命中或已过期，正在抓取详情页: {url}")
    html = fetch_page_for_request(url, dict(headers))
    if not html:
        return None
    
    parsed_data = EhParser.parse_gallery_detail(html)
    # 如果解析结果为空字典，说明解析失败
    if not parsed_data:
        logging.warning(f"画廊详情页 {url} 解析结果为空。")
        # EhParser 内部已记录 HTML，这里只记录上下文
        return None
        
    return parsed_data

@cached(cache=gallery_cache)
def get_gallery_images_data(gid: int, token: str, page: int, headers: tuple, url_builder: 'EhUrlBuilder', start_index: int = 0, limit: Optional[int] = None):
    url = f"{url_builder.build_gallery_url(gid=gid, token=token)}?p={page}"
    logging.info(f"缓存未命中或已过期，正在抓取图片列表并解析指定范围大图: {url}")
    preview_html = fetch_page_for_request(url, dict(headers))
    if not preview_html:
        return None
        
    preview_list = EhParser.parse_preview_images(preview_html)
    if not preview_list:
        logging.warning(f"画廊图片预览页 {url} 解析结果为空列表。")
        return []

    if limit is not None:
        preview_list = preview_list[start_index:start_index + limit]
    elif start_index > 0:
        preview_list = preview_list[start_index:]

    final_images = [None] * len(preview_list)
    def fetch_and_parse_image_url(position, preview_item):
        page_html = fetch_page_for_request(preview_item['page_url'], dict(headers))
        if page_html:
            image_url = EhParser.parse_image_page(page_html)
            if image_url:
                return position, image_url
        logging.warning(f"无法从 {preview_item['page_url']} 获取最终图片链接。")
        return position, None

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
        future_to_url = {executor.submit(fetch_and_parse_image_url, index, item): item for index, item in enumerate(preview_list)}
        for future in as_completed(future_to_url):
            original_item = future_to_url[future]
            try:
                index, image_url = future.result()
                if image_url:
                    crop_info = f"&crop_x={original_item['crop_x']}&crop_y={original_item['crop_y']}&crop_w={original_item['crop_w']}&crop_h={original_item['crop_h']}"
                    thumbnail_proxy_url = f"/image/proxy?url={original_item['thumbnail_url']}{crop_info}&w={THUMBNAIL_PROXY_WIDTH}&q={THUMBNAIL_PROXY_QUALITY}"
                    final_images[index] = {'index': index, 'thumbnail_jpg': thumbnail_proxy_url, 'image_jpg': f"/image/proxy?url={image_url}"}
            except Exception as exc: logging.error(f"并发任务生成异常: {exc}")
    return [img for img in final_images if img is not None]


def get_virtual_chapter_images_data(gid: int, token: str, chapter: int, headers: tuple, url_builder: 'EhUrlBuilder'):
    chapter = max(1, chapter)
    start = (chapter - 1) * VIRTUAL_CHAPTER_SIZE
    remaining = VIRTUAL_CHAPTER_SIZE
    eh_page = start // EH_PREVIEW_PAGE_SIZE
    offset = start % EH_PREVIEW_PAGE_SIZE
    images = []

    while remaining > 0:
        page_limit = min(remaining, EH_PREVIEW_PAGE_SIZE - offset)
        page_images = get_gallery_images_data(gid, token, eh_page, headers, url_builder, offset, page_limit)
        if page_images is None:
            return None
        images.extend(page_images)
        if len(page_images) < page_limit:
            break
        remaining -= len(page_images)
        eh_page += 1
        offset = 0

    return images


@cached(cache=image_proxy_cache)
def get_processed_image_data(url: str, headers: tuple, max_width: int, quality: int, crop_params: Optional[tuple] = None, output_format: str = "jpeg"):
    logging.info(f"图片缓存未命中或已过期，正在处理图片: {url}")
    try:
        response = requests.get(url, headers=dict(headers), timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        crop_dict = None
        if crop_params: crop_dict = {'x': crop_params[0], 'y': crop_params[1], 'w': crop_params[2], 'h': crop_params[3]}
        result = ImageProcessor.process_and_compress(image_bytes=response.content, max_width=max_width, quality=quality, crop_params=crop_dict, output_format=output_format)
        return result[0] if result else None
    except requests.RequestException as e: logging.error(f"下载原始图片失败: {e}"); return None
    except Exception as e: logging.error(f"处理图片时发生未知错误: {e}"); return None

# ==============================================================================
# Flask 应用配置与路由
# ==============================================================================
REQUEST_TIMEOUT = 20; DEFAULT_PROXY_WIDTH = 400; DEFAULT_PROXY_QUALITY = 50
THUMBNAIL_PROXY_WIDTH = 150; THUMBNAIL_PROXY_QUALITY = 40
MAX_CONCURRENT_REQUESTS = 10
EH_PREVIEW_PAGE_SIZE = 20
VIRTUAL_CHAPTER_SIZE = 20

def parse_user_agent(user_agent: str) -> dict:
    device_info = {'product': '', 'brand': '', 'os_type': '', 'os_version': '', 'language': '', 'region': ''}
    try:
        if not user_agent:
            return device_info
        
        parts = user_agent.split('/')
        if len(parts) >= 5:
            device_info['product'] = parts[1] if len(parts) > 1 else ''
            device_info['brand'] = parts[2] if len(parts) > 2 else ''
            device_info['os_type'] = parts[3] if len(parts) > 3 else ''
            device_info['os_version'] = parts[4] if len(parts) > 4 else ''
            device_info['language'] = parts[6] if len(parts) > 6 else ''
            device_info['region'] = parts[7] if len(parts) > 7 else ''
    except Exception as e:
        logging.warning(f"解析 User-Agent 失败: {e}")
    
    return device_info

def parse_id(id_str: str) -> tuple:
    try:
        if '_' in id_str:
            parts = id_str.split('_')
            if len(parts) == 2:
                gid_str, token = parts
                if re.match(r'^\d+$', gid_str) and re.match(r'^[a-f0-9]+$', token):
                    return int(gid_str), token
        return None, None
    except Exception as e:
        logging.warning(f"解析 ID 失败: {e}")
        return None, None



def fetch_page_for_request(url: str, headers: dict) -> Optional[str]:
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        if 'exhentai.org' in url:
            logging.warning(f"ExHentai 请求失败，尝试回退到 E-Hentai: {e}, URL: {url}")
            fallback_url = url.replace('exhentai.org', 'e-hentai.org')
            try:
                response = requests.get(fallback_url, headers=headers, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                logging.info(f"成功回退到 E-Hentai: {fallback_url}")
                return response.text
            except requests.RequestException as fallback_e:
                logging.error(f"E-Hentai 回退也失败: {fallback_e}, URL: {fallback_url}")
                return None
        else:
            logging.error(f"请求 E-Hentai 失败: {e}, URL: {url}")
            return None
