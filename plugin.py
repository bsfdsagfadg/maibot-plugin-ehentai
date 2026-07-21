import base64
import asyncio
import zipfile
import os
import tempfile
from typing import Any
from urllib.parse import urlencode

from maibot_sdk import MaiBotPlugin, Tool, PluginConfigBase, Field
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from . import eh_api

class EHentaiPluginSection(PluginConfigBase):
    __ui_label__ = "E-Hentai 设置"
    __ui_icon__ = "api"
    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用")
    cookie: str = Field(default="", description="用于访问 ExHentai 的 Cookie (igneous, ipb_member_id 等)")
    proxy: str = Field(default="", description="HTTP/HTTPS 代理地址，例如 http://127.0.0.1:7890，留空则不使用代理")
    proxy_width: int = Field(default=400, description="图片缩放宽度（影响内存占用）")
    proxy_quality: int = Field(default=50, description="图片压缩质量 (1-100)")
    request_timeout: float = Field(default=30.0, description="请求超时时间(秒)")
    user_agent: str = Field(default="MaiBot(1.0.0)/Server/Host/Linux/1.0/1.0/zh/CN", description="请求 User-Agent (包含 zh/CN 会触发标签汉化)")

class EHentaiConfig(PluginConfigBase):
    plugin: EHentaiPluginSection = Field(default_factory=EHentaiPluginSection)

class EHentaiPlugin(MaiBotPlugin):
    config_model = EHentaiConfig


    async def on_load(self) -> None:
        self.ctx.logger.info("E-Hentai 插件已加载")
        eh_api.PROXY_URL = self.config.plugin.proxy
        eh_api.plugin_logger = self.ctx.logger
        eh_api.REQUEST_TIMEOUT = self.config.plugin.request_timeout
        self.download_cache = {}
        self._bg_tasks = set()

    async def on_unload(self) -> None:
        self.ctx.logger.info("E-Hentai 插件已卸载")
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == "self":
            eh_api.REQUEST_TIMEOUT = self.config.plugin.request_timeout
            eh_api.PROXY_URL = self.config.plugin.proxy
            self.ctx.logger.info("E-Hentai 配置已更新")

    def _get_headers_tuple(self) -> tuple:
        headers = {"User-Agent": self.config.plugin.user_agent}
        if self.config.plugin.cookie:
            headers["Cookie"] = self.config.plugin.cookie
        return tuple(headers.items())

    @Tool(
        "eh_search",
        brief_description="搜索 E-Hentai 画廊",
        detailed_description="搜索 E-Hentai 画廊。支持高级语法如：\n- language:chinese (中文内容)\n- parody:touhou (东方同人)\n支持分页(page)检索。",
        parameters=[
            ToolParameterInfo(name="query", param_type=ToolParamType.STRING, description="搜索关键词", required=True),
            ToolParameterInfo(name="page", param_type=ToolParamType.INTEGER, description="页码，默认 1", required=False, default=1),
        ]
    )
    async def eh_search(self, query: str, page: int = 1, **kwargs):
        def _do():
            url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
            search_url = url_builder.build_search_url(keyword=query)
            if page > 1:
                search_url += f"&page={page - 1}"
            parsed = eh_api.get_gallery_list_data(search_url, self._get_headers_tuple())
            if not parsed:
                return {"success": False, "error": "搜索失败或无结果"}
            results = []
            for g in parsed.get('galleries', []):
                results.append({
                    "gallery_id": f"{g['gid']}_{g['token']}",
                    "title": g.get('title', ''),
                    "pages": g.get('pages', 0)
                })
            pagination = parsed.get('pagination', {})
            return {"success": True, "page": page, "has_more": pagination.get('has_next', False), "results": results}
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Tool(
        "eh_popular",
        brief_description="获取当前热门画廊列表",
        detailed_description="查询 E-Hentai/ExHentai 当前的热门画廊推荐。",
        parameters=[]
    )
    async def eh_popular(self, **kwargs):
        def _do():
            url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
            parsed = eh_api.get_gallery_list_data(url_builder.build_popular_url(), self._get_headers_tuple())
            if not parsed: return {"success": False, "error": "获取热门画廊失败"}
            results = [{"gallery_id": f"{g['gid']}_{g['token']}", "title": g.get('title', ''), "pages": g.get('pages', 0)} for g in parsed.get('galleries', [])]
            return {"success": True, "results": results}
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Tool(
        "eh_get_balance",
        brief_description="获取账户的货币余额 (GP, Hath, Credits)",
        detailed_description="查询当前账号在 E-Hentai/ExHentai 的货币余额，包含 GP、Hath 以及 Credits，需要配置 Cookie。",
        parameters=[]
    )
    async def eh_get_balance(self, **kwargs):
        if not self.config.plugin.cookie:
            return {"success": False, "error": "请先在配置中填入 Cookie 以查询余额"}
        def _do():
            base_url = "https://e-hentai.org"
            headers = dict(self._get_headers_tuple())
            hath_res = eh_api.http_request('GET', f"{base_url}/exchange.php?t=hath", headers=headers)
            hath_res.raise_for_status()
            balances = eh_api.EhParser.parse_exchange_balances(hath_res.text)
            gp_res = eh_api.http_request('GET', f"{base_url}/exchange.php?t=gp", headers=headers)
            gp_res.raise_for_status()
            balances.update(eh_api.EhParser.parse_exchange_balances(gp_res.text))
            final_balances = {
                "Credits": int(balances.get("Credits", 0)),
                "Hath": int(balances.get("Hath", 0)),
                "GP": int(balances.get("kGP", 0) * 1000)
            }
            return {"success": True, "balances": final_balances}
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}
    @Tool(
        "eh_get_favorites",
        brief_description="获取当前账号的收藏夹画廊",
        detailed_description="读取当前配置账号的云端收藏夹。可指定 favcat (0-9或'all') 和 page (默认 0)。",
        parameters=[
            ToolParameterInfo(name="favcat", param_type=ToolParamType.STRING, description="分类 0-9 或 'all'", required=False, default="all"),
            ToolParameterInfo(name="page", param_type=ToolParamType.INTEGER, description="页码，默认 0", required=False, default=0)
        ]
    )
    async def eh_get_favorites(self, favcat: str = "all", page: int = 0, **kwargs):
        if not self.config.plugin.cookie:
            self.ctx.logger.error("[eh_get_favorites] 未配置 Cookie")
            return {"success": False, "error": "请先在配置中填入 Cookie 以使用收藏夹功能"}
        def _do():
            url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
            parsed = eh_api.get_gallery_list_data(url_builder.build_favorites_url(favcat=str(favcat), page=page), self._get_headers_tuple())
            if not parsed: return {"success": False, "error": "获取收藏夹失败"}
            results = [{"gallery_id": f"{g['gid']}_{g['token']}", "title": g.get('title', ''), "pages": g.get('pages', 0)} for g in parsed.get('galleries', [])]
            return {"success": True, "page": page, "results": results}
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Tool(
        "eh_get_detail",
        brief_description="获取画廊元数据与标签",
        detailed_description="获取画廊详情，包含标签（tags）、评分（rating）、总页数（pages）、评论区及虚拟章节数。",
        parameters=[ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID (格式: gid_token)", required=True)]
    )
    async def eh_get_detail(self, gallery_id: str, **kwargs):
        def _do():
            gid, token = eh_api.parse_id(gallery_id)
            if not gid: return {"success": False, "error": "ID 格式错误"}
            url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
            parsed = eh_api.get_gallery_detail_data(gid, token, self._get_headers_tuple(), url_builder)
            if not parsed: return {"success": False, "error": "获取详情失败"}
            device_info = eh_api.parse_user_agent(self.config.plugin.user_agent)
            translate_tags = eh_api.is_chinese_locale(device_info)
            flat_tags = eh_api.flatten_eh_tags(parsed, translate=translate_tags)
            cover_url = parsed.get('thumbnail')
            content_items = []
            if cover_url:
                processed_bytes = eh_api.get_processed_image_data(cover_url, self._get_headers_tuple(), self.config.plugin.proxy_width, self.config.plugin.proxy_quality)
                if processed_bytes:
                    content_items.append({"type": "image", "data": base64.b64encode(processed_bytes).decode("ascii"), "mime_type": "image/jpeg", "name": "cover.jpg"})
            return {
                "success": True,
                "gallery_id": gallery_id,
                "title": parsed.get('title') or parsed.get('title_jp'),
                "rating": parsed.get('rating'),
                "page_count": parsed.get('pages'),
                "tags": flat_tags,
                "total_chapters": max(1, (parsed.get('pages', 0) + eh_api.VIRTUAL_CHAPTER_SIZE - 1) // eh_api.VIRTUAL_CHAPTER_SIZE),
                "comments": parsed.get('comments', []),
                "cover_url": cover_url,
                "content_items": content_items
            }
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Tool(
        "eh_read_chapter",
        brief_description="提交画廊章节后台下载任务",
        detailed_description="提交后台异步下载画廊章节图片。下载完成后会向当前聊天流发送系统通知，收到通知后使用 eh_check_chapter_download 工具获取图片。",
        parameters=[
            ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID", required=True),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.INTEGER, description="章节号 (默认1)", required=False, default=1)
        ]
    )
    async def eh_read_chapter(self, gallery_id: str, chapter: int = 1, **kwargs):
        stream_id = kwargs.get("stream_id")
        if not stream_id: return {"success": False, "error": "缺少 stream_id"}
        cache_key = f"{gallery_id}_{chapter}"
        
        if cache_key in self.download_cache:
            status = self.download_cache[cache_key]["status"]
            if status == "done":
                return {"success": True, "message": f"该章节已下载完成。你可以调用 eh_check_chapter_download 获取第 {chapter} 章的图片！"}
            elif status == "downloading":
                return {"success": True, "message": "该章节正在后台下载中，请等待完成通知。"}
                
        self.download_cache[cache_key] = {"status": "downloading", "items": []}
            
        async def background_task():
            try:
                gid, token = eh_api.parse_id(gallery_id)
                if not gid: raise ValueError("ID 格式错误")
                url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
                images = eh_api.get_virtual_chapter_images_data(gid, token, chapter, self._get_headers_tuple(), url_builder)
                if not images: raise ValueError(f"章节 {chapter} 获取失败或无图片")

                async def fetch_and_process(i, img):
                    def _do_process():
                        raw_url = img['image_jpg'].replace("/image/proxy?url=", "").split('&w=')[0]
                        processed_bytes = eh_api.get_processed_image_data(
                            raw_url, self._get_headers_tuple(), self.config.plugin.proxy_width, self.config.plugin.proxy_quality
                        )
                        if processed_bytes:
                            return {"type": "image", "data": base64.b64encode(processed_bytes).decode("ascii"), "mime_type": "image/jpeg", "name": f"page_{i+1}.jpg"}
                        return None
                    return await asyncio.to_thread(_do_process)

                content_items = await asyncio.gather(*[fetch_and_process(i, img) for i, img in enumerate(images)])
                content_items = [item for item in content_items if item is not None]
                self.download_cache[cache_key] = {"status": "done", "items": content_items}
                await self.ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": f"画廊 {gallery_id} 第 {chapter} 章后台下载完成，你可以使用 eh_check_chapter_download 获取图片。"}],
                    visible_text=f"画廊 {gallery_id} 第 {chapter} 章下载完成"
                )
            except Exception as e:
                self.download_cache[cache_key] = {"status": "error", "error": str(e)}
                self.ctx.logger.error(f"后台下载失败: {e}")
                await self.ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": f"画廊 {gallery_id} 第 {chapter} 章后台下载失败: {e}"}],
                    visible_text=f"画廊 {gallery_id} 第 {chapter} 章下载失败"
                )

        task = asyncio.create_task(background_task())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {"success": True, "content": f"画廊 {gallery_id} 第 {chapter} 章的下载任务已提交至后台。等待下载完成通知。"}

    @Tool(
        "eh_check_chapter_download",
        brief_description="获取已完成的画廊章节图片",
        detailed_description="在 eh_read_chapter 后台任务完成后调用此工具。将提取缓存的章节图片载入视觉上下文。",
        parameters=[
            ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID", required=True),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.INTEGER, description="章节号 (默认1)", required=False, default=1)
        ]
    )
    async def eh_check_chapter_download(self, gallery_id: str, chapter: int = 1, **kwargs):
        cache_key = f"{gallery_id}_{chapter}"
        if cache_key not in self.download_cache: return {"success": False, "error": f"未找到画廊 {gallery_id} 第 {chapter} 章的下载任务记录。"}
        entry = self.download_cache[cache_key]
        if entry["status"] == "downloading": return {"success": False, "error": "该章节正在后台下载，请等待完成通知。"}
        elif entry["status"] == "error": return {"success": False, "error": f"该章节下载已失败: {entry.get('error')}"}
        items = entry.get("items", [])
        return {"success": True, "content": f"画廊 {gallery_id} 第 {chapter} 章读取成功，共 {len(items)} 张图片。", "content_items": items}

    @Tool(
        "eh_read_previews",
        brief_description="获取画廊单页缩略预览图",
        detailed_description="同步获取画廊指定页的缩略图（通常20-40张）。无需等待后台任务，适用于快速概览画风及内容。",
        parameters=[
            ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID", required=True),
            ToolParameterInfo(name="page", param_type=ToolParamType.INTEGER, description="预览页码 (默认0)", required=False, default=0)
        ]
    )
    async def eh_read_previews(self, gallery_id: str, page: int = 0, **kwargs):
        def _do():
            gid, token = eh_api.parse_id(gallery_id)
            if not gid: raise ValueError("ID 格式错误")
            url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
            url = f"{url_builder.build_gallery_url(gid=gid, token=token)}?p={page}"
            headers = self._get_headers_tuple()
            preview_html = eh_api.fetch_page_for_request(url, dict(headers))
            if not preview_html: return {"success": False, "error": "获取预览页面失败"}
            preview_list = eh_api.EhParser.parse_preview_images(preview_html)
            if not preview_list: return {"success": False, "error": "当前页没有预览图"}
            
            from concurrent.futures import ThreadPoolExecutor
            def process_thumbnail(item):
                crop_params = (item['crop_x'], item['crop_y'], item['crop_w'], item['crop_h'])
                processed_bytes = eh_api.get_processed_image_data(
                    item['thumbnail_url'], headers, eh_api.THUMBNAIL_PROXY_WIDTH, eh_api.THUMBNAIL_PROXY_QUALITY, crop_params
                )
                if processed_bytes: return {"type": "image", "data": base64.b64encode(processed_bytes).decode("ascii"), "mime_type": "image/jpeg", "name": f"preview_{item['index']}.jpg"}
                return None
            with ThreadPoolExecutor(max_workers=eh_api.MAX_CONCURRENT_REQUESTS) as executor:
                results = list(executor.map(process_thumbnail, preview_list))
            content_items = [r for r in results if r is not None]
            return {"success": True, "content": f"画廊 {gallery_id} 第 {page} 页预览缩略图获取成功，共 {len(content_items)} 张。", "content_items": content_items}
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Tool(
        "eh_archive_download",
        brief_description="使用 GP 下载重采样版画廊归档",
        detailed_description="通过 E-Hentai Archiver 消耗 GP 下载画廊（强制重采样版）。下载并解压后将永久缓存，后续必须使用 eh_read_archive 分批读取图片。",
        parameters=[ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID (格式: gid_token)", required=True)]
    )
    async def eh_archive_download(self, gallery_id: str, **kwargs):
        stream_id = kwargs.get("stream_id")
        if not stream_id: return {"success": False, "error": "缺少 stream_id"}
        async def background_task():
            try:
                gid, token = eh_api.parse_id(gallery_id)
                if not gid: raise ValueError("ID 格式错误")
                archive_dir = self.ctx.paths.data_dir / "archives" / f"{gid}_{token}"
                if archive_dir.exists() and any(archive_dir.iterdir()):
                    await self.ctx.maisaka.context.append(
                        stream_id=stream_id,
                        segments=[{"type": "text", "content": f"画廊 {gallery_id} 已在永久缓存中，可使用 eh_read_archive 提取图片。"}],
                        visible_text=f"画廊 {gallery_id} 命中本地缓存"
                    )
                    return
                archive_dir.mkdir(parents=True, exist_ok=True)
                url_builder = eh_api.EhUrlBuilder(use_exhentai=bool(self.config.plugin.cookie))
                archiver_url = f"{url_builder.base_url}/archiver.php?gid={gid}&token={token}"
                def _do_archiver_req():
                    headers = dict(self._get_headers_tuple())
                    res = eh_api.http_request('GET', archiver_url, headers=headers)
                    res.raise_for_status()
                    form_action = eh_api.EhParser.parse_archiver_form_url(res.text)
                    if not form_action: raise ValueError("未能找到下载表单。GP 可能不足或画廊不支持 Archiver。")
                    data = {"dltype": "res", "dlcheck": "Download Resample Archive"}
                    post_res = eh_api.http_request('POST', form_action, data=data, headers=headers)
                    post_res.raise_for_status()
                    
                    download_link = eh_api.EhParser.get_archiver_download_url(post_res.text)
                    if not download_link: raise ValueError("未能获取真实的下载直链。")
                    
                    # 如果这是一个重定向页（Hath network 准备页面），获取下一级的真正下载链接
                    if "hath.network" in download_link or "exhentai.org/archive/" in download_link:
                        prep_res = eh_api.http_request('GET', download_link, headers=headers)
                        prep_res.raise_for_status()
                        real_link = eh_api.EhParser.get_archiver_download_url(prep_res.text)
                        if real_link:
                            # 处理相对路径
                            if real_link.startswith('/'):
                                from urllib.parse import urlparse
                                parsed_uri = urlparse(download_link)
                                download_link = f"{parsed_uri.scheme}://{parsed_uri.netloc}{real_link}"
                            else:
                                download_link = real_link
                                
                    return download_link
                
                download_link = await asyncio.to_thread(_do_archiver_req)
                
                def _download_and_extract():
                    with eh_api.http_request('GET', download_link, headers=dict(self._get_headers_tuple()), stream=True) as res:
                        res.raise_for_status()
                        temp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tf:
                                temp_path = tf.name
                                for chunk in res.iter_content(chunk_size=8192): tf.write(chunk)
                            with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                                for member in zip_ref.namelist():
                                    member_path = os.path.normpath(member)
                                    if os.path.isabs(member_path) or member_path.startswith('..'):
                                        continue
                                    zip_ref.extract(member, str(archive_dir))
                        finally:
                            if temp_path and os.path.exists(temp_path):
                                os.remove(temp_path)
                
                await asyncio.to_thread(_download_and_extract)
                await self.ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": f"画廊 {gallery_id} (重采样版) 已成功缓存至本地，请使用 eh_read_archive 读取。"}],
                    visible_text=f"画廊 {gallery_id} 归档下载完成"
                )
            except Exception as e:
                self.ctx.logger.error(f"Archive 下载失败: {e}")
                await self.ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": f"画廊 {gallery_id} 归档下载失败: {e}"}],
                    visible_text=f"画廊 {gallery_id} 归档下载失败"
                )
        task = asyncio.create_task(background_task())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {"success": True, "content": f"画廊 {gallery_id} 重采样归档下载任务已在后台启动。处理解压可能耗时，请告知用户稍候并等待通知。"}

    @Tool(
        "eh_read_archive",
        brief_description="读取已永久缓存的画廊归档图片",
        detailed_description="配合 eh_archive_download 使用。提取本地已永久缓存的画廊归档。支持 offset 和 limit 参数实现分页读取，建议单次请求 limit 保持在 20 张以内。",
        parameters=[
            ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID", required=True),
            ToolParameterInfo(name="offset", param_type=ToolParamType.INTEGER, description="从第几张开始读取 (默认 0)", required=False, default=0),
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="每次最多读取几张 (默认 20)", required=False, default=20)
        ]
    )
    async def eh_read_archive(self, gallery_id: str, offset: int = 0, limit: int = 20, **kwargs):
        def _do():
            gid, token = eh_api.parse_id(gallery_id)
            if not gid: return {"success": False, "error": "ID 格式错误"}
            archive_dir = self.ctx.paths.data_dir / "archives" / f"{gid}_{token}"
            if not archive_dir.exists(): return {"success": False, "error": f"未找到画廊 {gallery_id} 的本地归档缓存，请先执行 eh_archive_download。"}
            
            image_files = []
            for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']: image_files.extend(archive_dir.glob(f"*{ext}"))
            for ext in ['.JPG', '.JPEG', '.PNG', '.GIF', '.WEBP']: image_files.extend(archive_dir.glob(f"*{ext}"))
            image_files = sorted(list(set(image_files)), key=lambda p: p.name)
            
            if not image_files: return {"success": False, "error": "缓存目录中没有找到图片。"}
            if offset >= len(image_files): return {"success": False, "error": f"偏移量 {offset} 超出范围，缓存共 {len(image_files)} 张图。"}
            
            chunk = image_files[offset:offset+limit]
            content_items = []
            for img_path in chunk:
                with open(img_path, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
                    ext = img_path.suffix.lower()
                    mime_type = "image/png" if ext == ".png" else ("image/gif" if ext == ".gif" else ("image/webp" if ext == ".webp" else "image/jpeg"))
                    content_items.append({"type": "image", "data": data, "mime_type": mime_type, "name": img_path.name})
            return {"success": True, "total": len(image_files), "loaded": len(chunk), "content_items": content_items, "message": f"成功读取本地归档图片，当前批次包含第 {offset} 到 {offset+len(chunk)-1} 张。"}
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Tool(
        "eh_forward_archive",
        brief_description="将本地缓存的画廊归档作为合并转发消息发送",
        detailed_description="配合 eh_archive_download 使用。提取本地已永久缓存的画廊归档图片并将其作为合并转发消息发送到当前消息流中，避免在普通聊天中出现刷屏。",
        parameters=[
            ToolParameterInfo(name="gallery_id", param_type=ToolParamType.STRING, description="画廊 ID", required=True),
            ToolParameterInfo(name="offset", param_type=ToolParamType.INTEGER, description="从第几张开始读取 (默认 0)", required=False, default=0),
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="每次最多发送几张 (默认 20)", required=False, default=20)
        ]
    )
    async def eh_forward_archive(self, gallery_id: str, offset: int = 0, limit: int = 20, **kwargs):
        stream_id = kwargs.get("stream_id")
        if not stream_id: return {"success": False, "error": "缺少 stream_id，无法发送合并转发"}
        def _do():
            gid, token = eh_api.parse_id(gallery_id)
            if not gid: raise ValueError("ID 格式错误")
            archive_dir = self.ctx.paths.data_dir / "archives" / f"{gid}_{token}"
            if not archive_dir.exists(): raise ValueError(f"未找到画廊 {gallery_id} 的本地归档缓存，请先执行 eh_archive_download。")
            
            image_files = []
            for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']: image_files.extend(archive_dir.glob(f"*{ext}"))
            for ext in ['.JPG', '.JPEG', '.PNG', '.GIF', '.WEBP']: image_files.extend(archive_dir.glob(f"*{ext}"))
            image_files = sorted(list(set(image_files)), key=lambda p: p.name)
            
            if not image_files: raise ValueError("缓存目录中没有找到图片。")
            if offset >= len(image_files): raise ValueError(f"偏移量 {offset} 超出范围，缓存共 {len(image_files)} 张图。")
            
            chunk = image_files[offset:offset+limit]
            forward_messages = []
            import io
            from PIL import Image
            for img_path in chunk:
                try:
                    with Image.open(img_path) as img:
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                        buffer = io.BytesIO()
                        img.save(buffer, format="JPEG", quality=75)
                        data = base64.b64encode(buffer.getvalue()).decode("ascii")
                except Exception:
                    with open(img_path, "rb") as f:
                        data = base64.b64encode(f.read()).decode("ascii")
                
                forward_messages.append({
                    "user_nickname": "E-Hentai Archiver",
                    "content": [
                        {"type": "text", "content": f"Page: {img_path.name}\n"},
                        {"type": "image", "content": data}
                    ]
                })
            return forward_messages, len(image_files), len(chunk)
            
        try:
            forward_messages, total, loaded = await asyncio.to_thread(_do)
            await self.ctx.send.forward(messages=forward_messages, stream_id=stream_id)
            return {"success": True, "total": total, "loaded": loaded, "message": f"成功读取本地归档图片并已作为合并转发发送，当前批次包含第 {offset} 到 {offset+loaded-1} 张。"}
        except Exception as e:
            return {"success": False, "error": str(e)}

def create_plugin() -> EHentaiPlugin:
    return EHentaiPlugin()
