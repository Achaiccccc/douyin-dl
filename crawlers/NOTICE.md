本目录 `crawlers/` 中的抖音 Web 解析代码来自
Evil0ctal/Douyin_TikTok_Download_API（Apache License 2.0）：

https://github.com/Evil0ctal/Douyin_TikTok_Download_API

仅内嵌「抖音单条作品解析」所需子集（web crawler + a_bogus / x_bogus），
未包含 TikTok、B 站、PyWebIO 前端。运行时 Cookie 由本项目的
`data/cookie.txt` 注入（仓库内为已过期的格式示例），不使用上游仓库
config.yaml 里的示例 Cookie。
