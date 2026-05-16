# 最终验收记录

更新时间: 2026-05-13

## 已完成

- 第三方 API Profile 与官方账号快照已分离，Claude Code / Codex CLI 均支持 API 与账号两类切换。
- 切换前预览、静态配置健康检查、API 连接测试、模型刷新和 provider 专属提示已接入。
- SSH 服务器支持把本机第三方 API 或官方账号快照推送到远程环境，并支持远程第三方 API 配置拉取。
- SSH 远程路径已支持 `~`、`$HOME`、自定义 Claude/Codex 目录和常见 Linux HOME 解析 fallback。
- SSH 远端自动续跑已支持 Claude/Codex 的检查、安装/修复、暂停、卸载；远端安装使用独立脚本和设置文件，失败会回滚。
- 浏览器 Profile 支持本机隔离启动、站点数据清理、整目录重置、托管 Profile 跨机器加密迁移。
- 浏览器启动已固定独立 `user-data-dir`、`Default` 分区、窗口尺寸和语言代码；清理时会尽量按实际 `--user-data-dir` 精确判断占用。
- Profile 迁移包使用密码加密，包含第三方 API/SSH 元数据、密钥和托管浏览器登录数据；同名导入会替换，非同名会合并。
- 数据目录已迁移到稳定用户配置目录，并支持自定义目录、便携模式、旧数据兼容迁移、原子写入和备份。
- 自动续跑、错误恢复、错误统计、Git 快照和回滚功能已完成基础集成。
- Codex compact 任务流式断开、上游 503 连接重置、网络断连等临时 API 错误已纳入自动恢复/续跑范围。
- 主窗口已改为按需加载非首屏页面，会话迁移扫描改为后台执行；普通启动会先显示轻量启动窗；打包脚本默认生成单文件 `dist/API切换器.exe`，并保留 `--onedir` 作为调试用文件夹版选项。
- 已完成源码编译、导入检查、错误恢复验证脚本、迁移逻辑 smoke test、PyInstaller 打包。

## 已知边界

- 浏览器 Profile 可以隔离 Cookies、本地存储、IndexedDB、缓存等站点数据，但不能保证跨机器被网页识别为完全相同设备；网页仍可能基于 IP、系统、显卡、字体、Canvas、WebGL、时区等生成指纹。
- Chromium Cookies 在 Windows 上可能受系统账号加密机制影响；迁移后如登录态失效，需要在新机器重新登录一次。
- SSH 同步和远端自动续跑依赖远程服务器权限、shell 行为、Python 3.6+ 和 Claude/Codex 安装路径；已做多路径兼容，远端自动续跑已用临时 Linux 服务器做过端到端验证。
- 错误恢复 Hook 需要在 GUI 的“通用设置”中启用后才会安装；未启用时验证脚本会报告 Hook 未安装。

## 仍需真实环境验证

- 使用真实 API Key 做端到端连接测试：DeepSeek、Kimi、GLM、自定义 OpenAI-compatible / Anthropic-compatible。
- 如要发布给更多用户，建议再用非 root 用户、不同 Linux 发行版和自定义 HOME/配置目录各跑一次 SSH 端到端验证。
- 使用真实 Chrome / Edge 托管 Profile 验证导出、导入、启动和站点登录态恢复。
