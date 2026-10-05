# v2.4.76 滚动性能优化检查

日期：2026-10-05。本版修复滚轮监听随已关闭窗口累积的问题，减少快速滚动中的重复处理，并保留嵌套列表、拖动滚动条和 DPI 缩放行为。没有改动代理路由、订阅、账号或 SSH 配置逻辑。

## 修复内容

- 每个 Tk 实例只注册一个滚轮分发器，按鼠标所在控件向外寻找可滚动区域。关闭弹窗后不再保留其全局滚轮与 Shift 监听；隐藏页面也不会逐个处理滚轮事件。
- 滚动引起的位置变化不再重复设置内容范围、调度文字换行计算；实际宽高或 DPI 变化仍正常处理。
- 内层滚动条已处理的滚轮事件不再带动外层页面。普通嵌套列表到达边界后仍可继续滚动外层。
- 列表缩短导致内容窗口暂时隐藏时及时修正滚动范围，避免停留在空白处；Shift 状态直接取自当前事件，避免切换焦点后横向滚动状态残留。

## 实测对比

Windows 11、Python 3.12.10、CustomTkinter 5.2.2，使用隔离合成数据和原生事件循环。每轮发送 640 个滚轮事件，比较模拟打开并关闭 24 个滚动窗口后的回调处理成本。节点页为 120 个长名称节点，账号页为 25 个 API 配置和 25 个账号快照，分流页为默认目标列表。

三个场景的滚轮分发回调均从 **16,000 次降至 640 次**，减少 96%。以下为这些回调的累计耗时，不是帧率或整个程序耗时：

| 场景 | 修复前 | 修复后 |
| --- | ---: | ---: |
| 节点列表 | 242.1 ms | 26.7 ms |
| 账号列表 | 237.3 ms | 24.4 ms |
| 目标分流 | 302.1 ms | 48.4 ms |

数据：[节点前](dist/scroll-performance-v2476/ui-perf-nodes-before.json)、[节点后](dist/scroll-performance-v2476/ui-perf-nodes-after.json)、[账号前](dist/scroll-performance-v2476/ui-perf-profiles-before.json)、[账号后](dist/scroll-performance-v2476/ui-perf-profiles-after.json)、[分流前](dist/scroll-performance-v2476/ui-perf-routes-before.json)、[分流后](dist/scroll-performance-v2476/ui-perf-routes-after.json)。修复前报告的 `global_wheel_bindings` 包含 Tcl 脚本空行；回调次数以 `slow_callbacks` 中的实际调用次数为准。

这些是单机合成场景，计时会受系统负载影响；没有据此承诺所有场景无卡顿。大量账号卡片首次创建仍有主线程绘制开销，本版重点解决持续滚动和反复开关窗口后的性能退化。

复测节点页：

```powershell
python tools/ui_performance_probe.py --scenario nodes --count 120 --long-names --scroll --scroll-history 24 --label check --run-id scroll-check --screenshot
```

## 验证范围

- 普通全量回归：**5074 passed、22 skipped**，302.54 秒。自动排除 40 个原生 GUI 模块，相关界面另行验证。
- 原生界面回归：8 个模块，共 **128 passed**。其中新增滚动回归 10 项，覆盖反复关闭 40 个窗口后的对象回收和监听数量、内外层边界、横向滚动、第三方监听保留、拖动、列表缩短及 100%、150%、250% 控件缩放。
- 其余已运行模块：渲染性能 37 项、简化分流流程 10 项、分流边界与确认 14 项、节点响应 11 项、节点分页 6 项、DPI 布局 23 项、智能预设简化界面 17 项。本轮没有重跑剩余 32 个原生 GUI 模块。
- 版本与打包契约、性能报告回归共 36 项通过；Python 语法、乱码检查、运行依赖、32 项固定版本依赖、Ruff 和 Git 差异格式检查通过。
- 完整窗口截图检查覆盖 Win11 100% 和 200% 缩放、SSH 150% 缩放的顶部、中部、底部；没有回调错误、控件横向越界或意外外部操作。

截图：[Win11 节点区](dist/scroll-performance-v2476/win-proxy-100.png)、[Win11 高 DPI 底部](dist/scroll-performance-v2476/win-proxy-200.png)、[SSH 节点区](dist/scroll-performance-v2476/ssh-proxy-150.png)、[节点分页](dist/scroll-performance-v2476/picker-pagination.png)。

测试使用隔离目录和合成数据，没有切换实际代理、刷新真实登录令牌或部署远端服务。现有下载目录中的运行进程保持不变；旧进程不会自动获得新版优化。

## 发布产物

- [Windows 单文件程序](dist/API切换器.exe)：FileVersion 和 ProductVersion 均为 2.4.76，31,325,586 字节。
- SHA-256：`B78A407D5127ACD63E57352D5C0D8148D732C8DCC39EA9C8CFFAFFCC8817D93B`。
- [构建日志](dist/scroll-performance-v2476/build.log)：单文件构建及 8 秒隔离启动检查通过。
- [内置代码核对](dist/scroll-performance-v2476/package-payload.log)：142 个项目模块及 main.py 与最终源码一致，仅归一化代码对象中的文件名。

需要启动本次生成的 EXE 才会使用这些优化；打包不会替换下载目录中正在运行的旧程序，也不会主动中断现有代理。
