# v2.4.77 列表刷新性能检查

日期：2026-10-06。本版减少 Claude/Codex API 与账号卡片刷新时的重绘和重新排布，保留现有操作方式。代理路由、订阅、账号存储和 SSH 部署逻辑不变。

## 改进内容

- 同名卡片的摘要或状态变化时，更新已有控件；不再销毁整张卡片并重新插入列表。新增、删除、改名和排序仍按实际变化处理。
- 操作集合未变时保留按钮，包括“测试中”的文字和禁用状态。账号失效时同步撤下切换和导出按钮；已保存的旧按钮回调也不能调用已撤销的操作。
- 更新异常后使显示缓存失效，刷新可以修复半更新状态，即使数据恢复成更新前的值也会重试。
- 复用信息标签与当前账号标记，避免反复编辑时增加换行监听。测试按钮直接登记，不再每次遍历卡片全部子控件。
- 节点分页截图回归改为捕获指定测试窗口，输出到临时目录；不再争抢 Windows 前台焦点或覆盖旧发布截图。布局断言保持不变。

## 实测结果

Windows 11、Python 3.12.10、CustomTkinter 5.2.2，使用隔离合成数据和原生事件循环。账号页包含 25 个 API 配置和 25 个账号快照；修改第一个 API 的模型摘要后对比：

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 列表就绪耗时 | 140.8 ms | 32.6 ms |
| 卡片渲染回调累计耗时 | 61.9 ms | 1.1 ms |
| 事件循环最大心跳间隔 | 109.2 ms | 31.6 ms |

这一场景的就绪耗时降低约 77%。数据：[修改前](dist/refresh-performance-v2477/profiles-before.json)、[修改后](dist/refresh-performance-v2477/profiles-after.json)。计时包括事件循环调度，受系统负载影响，不代表全程序提速比例或帧率。

200 个节点的列表也完成了筛选、清除筛选、选择、全选和连续滚动检查。全选范围为全部 200 个节点，而非仅当前页。模拟打开并关闭 24 个滚动窗口后，640 个滚轮事件仍只经过一个全局监听；账号页和节点页都没有回调错误。[节点实测数据](dist/refresh-performance-v2477/nodes-check.json)。这些是界面测试，不是外部节点连通性测试。

大量卡片首次创建仍有优化空间：50 张卡片在本轮实测中约需 5–6.5 秒，部分绘制会延后到下一次刷新。本版主要改善已有列表的更新，不承诺首次加载或所有场景均无卡顿。

复测命令：

```powershell
python tools/ui_performance_probe.py --scenario profiles --count 25 --scroll --scroll-history 24 --label check --run-id refresh-check --screenshot
python tools/ui_performance_probe.py --scenario nodes --count 200 --scroll --scroll-history 24 --label check --run-id refresh-check --screenshot
```

## 验证范围

- 普通全量回归：5080 项通过、22 项跳过，406.09 秒，自动排除 41 个原生 GUI 模块。[回归日志](dist/refresh-performance-v2477/regression.log)。首轮有一个延迟导入子进程以 `0xC000070A` 异常退出，无 Python 错误输出；相关检查单独复测和第二轮完整回归均通过，该退出尚未稳定复现。
- 原生界面回归：8 个模块共 118 项通过，覆盖卡片局部更新、旧操作撤销、更新中断与恢复、渲染性能、滚动、DPI、主窗口、节点响应和分页。其余 33 个原生 GUI 模块不在本次回归范围内。
- 版本与打包、发布检查和卡片列表契约：76 项通过；延迟导入相关检查 21 项通过；性能报告回归 8 项通过。
- 全项目 Ruff、Python 语法、乱码检查、运行依赖和 32 项固定版本依赖检查通过。
- Codex 100% 与 Claude 200% 缩放的完整窗口顶部、底部截图无横向控件越界或回调错误。卡片操作布局另覆盖 100%、150%、250% 控件缩放。

截图：[Codex 顶部](dist/refresh-performance-v2477/codex-top-100.png)、[Codex 账号区](dist/refresh-performance-v2477/codex-bottom-100.png)、[Claude 高 DPI 账号区](dist/refresh-performance-v2477/claude-bottom-200.png)。原始布局检查：[Codex](dist/refresh-performance-v2477/codex-100-report.json)、[Claude](dist/refresh-performance-v2477/claude-200-report.json)。

测试使用隔离目录和合成账号、节点，没有切换实际代理、刷新真实令牌或部署远端服务。下载目录中正在运行的旧程序保持不变。

## 发布产物

- [Windows 单文件程序](dist/API切换器.exe)：FileVersion 和 ProductVersion 均为 2.4.77，31,328,429 字节。
- SHA-256：`F6979626DE19B21E45D334CB0657B40AD35AED4333445D32CCE3D417CFD6D7ED`。
- [构建日志](dist/refresh-performance-v2477/build.log)：单文件构建及 8 秒隔离启动检查通过。
- [包内代码核对](dist/refresh-performance-v2477/package-payload.log)：142 个项目模块及 main.py 与最终源码一致，仅归一化代码对象中的文件名。

需启动这份新 EXE 才能使用优化；下载目录中的旧进程不会自动更新。本次构建不替换或停止正在运行的程序与代理。
