# Bing-search

基于必应（Bing）搜索引擎的 AstrBot 网页搜索插件，适用于暂时没有搜索引擎 API 的用户。

插件会注册一个名为 `web_search_bing` 的 LLM 工具，调用方式和返回格式都尽量贴近 AstrBot 原生网页搜索工具；同时提供 `/bing` 指令，方便手动测试或直接查资料。

## 特性

- **无需搜索 API Key**：直接使用 Bing 的公开搜索结果。
- **模拟原生工具**：输出与 AstrBot 内置 `web_search_*` 工具相同的 JSON 结构：

  ```json
  {
    "results": [
      {
        "title": "结果标题",
        "url": "https://example.com/",
        "snippet": "结果摘要",
        "index": "a1b2.1"
      }
    ]
  }
  ```

  模型可以继续使用 `index` 生成 `<ref>` 引用。
- **RSS 优先、HTML 回退**：先请求轻量的 Bing RSS 结果，失败或为空时自动解析网页结果。
- **全异步网络请求**：使用 `aiohttp`，不会阻塞 AstrBot 事件循环。
- **多市场支持**：可配置 `zh-CN`、`en-US`、`ja-JP` 等市场，自动同步 Bing 的 `mkt`、`setlang`、`cc` 参数。
- **可选代理 / Cookie**：默认遵循系统 `HTTP_PROXY`、`HTTPS_PROXY` 环境变量，也可以在配置中单独指定。
- **无本地文件依赖**：插件不读取、不写入本地文件，因此不需要处理 Windows / Linux 路径差异。

## 安装

### 通过 AstrBot 插件市场

在 WebUI 的「插件」页面搜索 `Bing 搜索` 或仓库地址并安装。

### 手动安装

把仓库克隆到 AstrBot 的 `data/plugins/` 目录，目录名建议使用 `astrbot_plugin_bing_search`：

```bash
cd AstrBot/data/plugins
git clone https://github.com/SATA-F5/Bing-search.git astrbot_plugin_bing_search
```

如果缺少依赖，在 AstrBot 使用的 Python 环境中安装：

```bash
pip install -r requirements.txt
```

安装后在 WebUI 的插件页面重载插件即可。

## 使用

### 1. 让模型自动搜索

插件加载后，`web_search_bing` 会出现在 AstrBot 的 LLM 工具列表中。模型在需要最新信息时会像调用原生网页搜索一样调用它，不需要额外指令。

工具参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `query` | string | 必填，搜索关键词 |
| `count` | integer | 可选，结果数量 1-10 |
| `market` | string | 可选，例如 `zh-CN`、`en-US` |
| `freshness` | string | 可选，`day`、`week`、`month` |

### 2. 使用指令手动搜索

```text
/bing AstrBot 插件开发
/必应 今天的科技新闻
/bsearch Python asyncio tutorial
```

指令默认显示 5 条结果，可在插件配置中修改。

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `register_llm_tool` | `true` | 是否注册 `web_search_bing` LLM 工具 |
| `enable_command` | `true` | 是否启用 `/bing` 指令 |
| `default_count` | `8` | LLM 工具默认结果数（1-10） |
| `command_count` | `5` | 指令默认结果数（1-10） |
| `market` | `zh-CN` | 默认搜索市场 |
| `safe_search` | `moderate` | 安全搜索：`moderate` / `strict` / `off` |
| `fetch_mode` | `auto` | `auto` / `rss` / `html` |
| `timeout` | `15.0` | 单次请求超时（秒） |
| `max_retries` | `2` | 超时、429、5xx 时的尝试次数 |
| `search_endpoint` | `https://www.bing.com/search` | 搜索入口，一般无需修改 |
| `proxy` | 空 | HTTP(S) 代理，留空则遵循系统代理环境变量 |
| `cookie` | 空 | 可选的 Bing Cookie |
| `user_agent` | 空 | 可选的 User-Agent |

## 工作原理

1. 请求 Bing 搜索接口，优先使用 `format=rss`。
2. RSS 结果为空或请求失败时，自动回退到 HTML 结果页并用 BeautifulSoup 解析。
3. 过滤广告块、解码 Bing 跳转链接。
4. 使用与 AstrBot 原生网页搜索相同的结构返回给模型。

Bing 的公开结果页单页通常返回约 10 条结果，因此插件的单次搜索上限为 10 条，避免额外分页请求拖慢响应。

## 兼容性

- AstrBot `>= 4.16, < 5`
- Python 3.10+
- Windows / Linux / WSL 均可运行
- 依赖：`aiohttp`、`beautifulsoup4`

## 注意事项

- 本插件通过公开搜索结果页获取内容，**不保证长期稳定**。Bing 可能根据地区、网络环境或频率返回跳转页、验证页或不同结构。
- 遇到结果为空时，可以尝试切换 `fetch_mode`、配置代理，或适当增大 `timeout`。
- 请遵守 Bing / Microsoft 的服务条款以及当地法律法规，仅将本插件用于个人、非商业的合理搜索场景。需要稳定、合规、可商用的搜索能力时，建议申请官方 Bing Search API 或其他搜索服务。
- 本插件与 Microsoft / Bing 官方无关联。

## 开发与测试

```bash
# 在 AstrBot 的插件目录中放置本仓库后启动 AstrBot
./run.sh

# 或后台启动
./run.sh -d
```

插件加载成功后，日志中会出现：

```text
Registered LLM tool: web_search_bing
```

随后可以在 WebUI 或聊天平台使用 `/bing` 验证搜索结果。

## 许可证

本项目使用 GNU Affero General Public License v3.0，详见 [LICENSE](LICENSE)。
