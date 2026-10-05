# v2.4.75 推荐分流界面发布检查

日期：2026-10-05。本版简化 Win11 与 SSH 共用的推荐分流弹窗，保留已有手动设置和实际应用前的安全检查，不改变后台分流规则。

## 界面变更

- 打开即显示 AI 和其他网站本次将修改的去向。订阅、预设和节点策略通过“调整方案”进入，原线路与新线路明细按需展开。
- 默认只补齐未配置目标。全部已有设置时显示“已保留现有分流”，不再强制进入无修改的核对步骤；“重新推荐已有分流”仅展示替换建议，取消不会写入草稿。
- 风险提示不随普通明细折叠：失效订阅、不可用节点、已有高风险手动节点及 AI 首选改选仍直接显示。AI 自动候选仍要求明确确认，不能保证出口 IP 或国家固定。
- 保留过期草稿和订阅变化拦截、严格隐私冲突校验及统一保存入口。“使用方案”只回填编辑器草稿，保存并应用后才切换线路。
- 缩短默认窗口高度，移除重复步骤导航和无意义的零计数；不可操作的按钮使用灰色。无有效方案时仍可取消退出。

## 验证范围

- [静态检查](dist/preset-result-first/static.log)：Python 语法、运行依赖、32 项固定版本依赖、乱码检查、Ruff 和 Git 差异格式通过。
- [普通全量回归](dist/preset-result-first/ordinary.log)：5070 passed、22 skipped，315.69 秒，一次通过；自动排除 39 个原生 GUI 模块，相关界面另行验证。
- [原生界面回归](dist/preset-result-first/verification.log)：三个相关模块分别 17、33、13 项通过，共 63 项。覆盖默认推荐、已有设置、重新推荐、直连保留、缺订阅、AI 自动换线确认、过期草稿、取消与错误退出，以及 100%、150%、250% 缩放场景。
- 本轮未重跑其余 36 个原生 GUI 模块。界面测试使用合成数据；没有修改正在使用的代理、账号、SSH 或客户端配置，也没有进行真实登录、远端部署或外网服务可用性验证。

## 隔离截图

- [推荐结果](dist/preset-result-first/preview-100.png)
- [已有设置保持不变](dist/preset-result-first/unchanged.png)
- [重新推荐的替换建议](dist/preset-result-first/replacement.png)
- [缺少订阅提示](dist/preset-result-first/missing.png)
- [高缩放窗口](dist/preset-result-first/preview-250.png)

## 发布产物

- [Windows 单文件 EXE](dist/API切换器.exe)：FileVersion 和 ProductVersion 均为 2.4.75，31,323,914 字节。
- SHA-256：`68C764A8EB9E623E97397BBD67A0FB7137B3151DD7B2B9265DABEEAB176FD654`。
- [构建日志](dist/preset-result-first/build.log)：单文件构建和 8 秒隔离启动检查通过。
- [内置代码核对](dist/preset-result-first/package-payload.log)：142 个项目模块及 main.py 与最终源码一致，仅归一化代码对象中的文件名。

“推荐”依据订阅标记和缓存，不等于已验证实时连通或出口质量。打包不会自动替换或关闭下载目录中正在运行的旧程序；查看新界面需要启动本次发布的 EXE。
