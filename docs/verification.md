# 本地验证记录

执行环境：Windows，Python 3.14.7，Node.js 24.11.1，Blender 5.1.1。以下结果只覆盖这套本机环境，其他平台与版本尚未验证。

- Python 单元测试：1 项通过。递归选择 PNG、排除缩略图和其他扩展名。
- Node 单元测试：2 项通过。Meshopt 解码逐字节一致、Three.js 几何和边界一致；不支持的 GLB 输入明确拒绝。
- 真实链路：`python tests/verify_pipeline.py --blender D:\Blender\blender.exe` 通过。5 张合成输入中，3 张在输出目录得到同名 GLB，源图旁得到 96×96 RGBA 透明 `_thumb.png`；不生成 `report.json`。其中一张位于嵌套子目录；空白 PNG 跳过；损坏 PNG 被文件头检查拒绝后仍继续处理后续文件，批次返回非零结果。
- 三张成功图的 `.work/` 诊断文件均标记 Meshopt `lossless=true`，各缓冲区 `exact=true`；宽度约 8 cm。原始 GLB、Blender 场景和日志保留。再次运行时已完成的三组结果被跳过，生成的 `_thumb.png` 未被当作新输入。
- 独立 Meshopt 命令行：使用真实生成的原始 GLB 执行成功，打印逐字节验证通过，并产生独立 GLB 与报告。
- 最新集成产物：`output/verification/20261022-182017-204878-47222d46/`，仅在本机保留，不纳入版本库。

当前仍需确认来源代码的公开再授权权利；未创建许可证、Git 仓库或公开发布。
