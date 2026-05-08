# 收尾验收与剩余外部验证

更新时间: 2026-05-08

## 已完成

- 已按官方文档更新 DeepSeek V4、Kimi、GLM 的 provider 预设、默认模型、端点、Codex wire API 与 Claude Code 环境变量映射。
- Codex 第三方 provider 会写入独立的 `[model_providers.<id>]`，避免 DeepSeek、Kimi、GLM 互相覆盖。
- 不支持推理力度的 provider 会自动移除 `model_reasoning_effort` / `effortLevel`，避免发送无效参数。
- Profile 存储已迁移到 version 4，并增加旧配置容错、坏数据跳过、原子写入和备份。
- 配置切换会清理旧 Profile 残留的密钥、权限白名单、额外目录和坏掉的 `model_providers` 结构，避免误用上一个配置。
- Python 3.10 环境已补充 `tomli` TOML 读取 fallback，本地和远程 Codex 配置读取都可用。
- 远程拉取 Claude 配置时会识别 DeepSeek/GLM provider，不再默认回落为 Anthropic。
- DPAPI fallback 密钥文件名已做 Windows 安全转义，Profile 名包含特殊字符时不会生成非法路径。
- Profile 编辑器已添加“测试连接”按钮，可校验端点、密钥和模型名。
- Profile 编辑器已添加“刷新模型”按钮，会优先从 provider 模型接口拉取，失败时回落到内置模型列表。
- Profile 编辑器已添加 provider 专属提示，会说明推理力度处理、wire API，以及 Kimi `.ai` / `.cn` 端点差异。
- UI 已完成一轮响应式与鲁棒性优化，Profile 编辑、导入、确认、健康检查、自动续跑设置等弹窗已做 smoke test。
- 已删除多余 Markdown 文档，仅保留本文件作为剩余事项记录。
- README 与项目说明已同步到当前 provider 支持范围和真实验收状态。
- 已完成源码编译、provider 回归、迁移回归、导入测试、错误恢复测试、图标生成和 exe 打包。

## 仍需真实环境验证

- 使用真实 API Key 做端到端连接测试:
  - DeepSeek: Codex OpenAI-compatible chat endpoint 与 Claude Code Anthropic-compatible endpoint
  - Kimi: Codex OpenAI-compatible chat endpoint；如果使用中国平台密钥，base_url 需要改为 `https://api.moonshot.cn/v1`
  - GLM: Coding Plan endpoint 与 Claude Code 环境变量映射
- 使用真实 SSH 服务器验证远程同步和远程拉取，重点确认 Codex 的 `[model_providers.<id>]` 写入。
- 如需启用错误自动恢复，需要在 GUI 中启用并安装 hook；当前本机验证显示 Claude/Codex 的 hook 尚未安装。
