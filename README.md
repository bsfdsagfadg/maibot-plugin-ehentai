# MaiBot E-Hentai 插件

这是一个专为 MaiBot 打造的第三方 E-Hentai/ExHentai 画廊搜刮与多模态阅读插件。基于强类型的 `PluginConfigBase` 架构与原生 MaiBot `Tool` 体系，支持零配置启动或完整的 ExHentai Cookie 鉴权。

## 功能特性

本插件提供了丰富的 E-Hentai 相关数据读取功能，均通过 MaiBot 系统的工具 (`@Tool`) 暴露给 AI，供 AI 自主规划并服务于用户。

- **`eh_search`**: 支持高级搜索语法的画廊检索功能（如 `language:chinese`）。
- **`eh_popular`**: 获取 E-Hentai 首页的热门榜单画廊。
- **`eh_get_favorites`**: 云端同步读取配置账号下的个人收藏夹分类。
- **`eh_get_detail`**: 深入解析指定画廊的元数据，包含封面、标签平铺、汉化翻译以及底部的原生评论区抓取。
- **`eh_get_balance`**: 查询并换算账号下的 GP、Hath 以及 Credits 的货币余额。
- **`eh_read_previews`**: 秒级极速提取整页缩略预览图，适用于快速扫掠画风与主要剧情。
- **`eh_read_chapter` & `eh_check_chapter_download`**: 支持异步后台并发下载并解析整个章节（默认 20 页/章），采用聊天流内消息无侵入通知，完美兼容 MaiBot 的 Focus 模式。
- **`eh_archive_download` & `eh_read_archive`**: GP 消耗接口。通过 E-Hentai Archiver 接口强制下载重采样版原图归档（zip），并在本机进行永久缓存提取。
- **`eh_forward_archive`**: 配合 `eh_archive_download` 使用。将已永久缓存的本地归档图片打包成合并转发消息发送，防止在普通聊天会话中多图刷屏。

## 安装与部署

1. 将本项目目录 `maibot-plugin-ehentai` 放置于 MaiBot 主程序的 `plugins/` 文件夹下。
2. 确保你的环境中安装了以下依赖（或由 MaiBot `ManifestValidator` 自动补齐）：
   - `requests` >= 2.28.0
   - `beautifulsoup4` >= 4.11.0
   - `pillow` >= 9.0.0
   - `cachetools` >= 5.0.0
3. 重新启动 MaiBot 主程序或在 WebUI 的插件管理中执行重载。

## 插件配置

你可以在 MaiBot 的 WebUI 插件设置页面中直接修改，也可以手动编辑插件目录下的 `config.toml` 文件：

```toml
[plugin]
config_version = "1.0.0"
enabled = true
cookie = "ipb_member_id=xxx; ipb_pass_hash=xxx; igneous=xxx"
proxy = "http://127.0.0.1:7890"
proxy_width = 400
proxy_quality = 50
request_timeout = 30.0
user_agent = "MaiBot(1.0.0)/Server/Host/Linux/1.0/1.0/zh/CN"
```

> **注意**：
> - 填入正确的 Cookie（含 `igneous`）后，插件将自动将请求域名提升至 `exhentai.org`。
> - 填写 `proxy` 即可实现代理连通，且支持后台热更新。
> - `user_agent` 中包含 `zh` 或 `CN` 会自动触发 EhTagTranslation 标签汉化映射机制。

## 开源与协议

本项目底层 API 逻辑解析部分借鉴自 [vela-py-eh-api-server](https://github.com/sf-yuzifu/vela-py-eh-api-server)。
由于上游项目采用 **AGPL-3.0** 协议，本插件仓库同样遵循且开源于 [AGPL-3.0 License](./LICENSE) 之下。

## 更新日志

### v1.0.1
- 新增默认配置文件 `config.toml`，开箱即用。
- 优化了插件的生命周期安全性，支持卸载及配置热重载时安全清理后台异步下载/解析任务，防止内存与资源泄漏。
- 完善了插件入口函数 `create_plugin` 的类型注解，使其符合 `maibot-plugin-sdk` 的推荐标准。
