# glbcreater

本地批量把平面图案 PNG 挤出为无贴图 GLB，生成 96×96 透明缩略图，再以 Meshopt 压缩并验证解码后的几何数据。适合透明背景图案、线稿和有限色块；启用 `--recover-cartoon-colors` 时会合并轻微阴影与过渡色，并尝试把渐变归并为最多 8 种主色。照片和复杂纹理仍可能被跳过，本工具不做照片三维重建。

## 本机安装

需要 Python 及虚拟环境、Node.js 和 Blender。当前已验证组合为 Python 3.14.7、Node.js 24.11.1、Blender 5.1.1；其他版本需自行验证。Windows 示例：

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    npm install

Linux 示例：

    python3 -m venv .venv
    ./.venv/bin/python -m pip install -r requirements.txt
    npm install

依赖安装在本项目内；Blender 由用户单独安装，本项目不捆绑其可执行程序。

## 转换

Windows 可把 PNG 放入根目录的 `images/`，双击 `run.bat`。Linux 使用 `sh run.sh`。两个脚本开头均可修改七项：输入目录、输出目录、模型宽度、挤出厚度、Blender 路径、`OVERWRITE` 和 `RECOVER_CARTOON_COLORS`。将 `OVERWRITE` 设为 `1` 才覆盖已有输出，默认 `0`；`RECOVER_CARTOON_COLORS` 默认 `1`，设为 `0` 可关闭卡通颜色和渐变主色恢复。宽度与厚度使用小数点。Linux 默认 Blender 路径为 `/usr/bin/blender`，安装在其他位置时先修改。Windows 脚本执行结束后窗口会停留，显示结果。首次运行前仍需完成上述项目依赖安装。

    .\.venv\Scripts\python.exe -m glb_tools --input D:\images --output D:\models --blender D:\Blender\blender.exe

输入目录递归扫描，仅处理后缀及文件头均为 PNG 的文件，排除文件名以 `_thumb` 结尾的缩略图。按相对路径排序，逐张显示分析、建模、压缩与验证阶段。默认图案宽度 8 cm、挤出厚度 0.5 cm；可用 `--width-cm`、`--thickness-cm`、`--color "#RRGGBB"`、`--recover-cartoon-colors` 和 `--overwrite` 调整。不开启 `--overwrite` 时不替换已有文件，只生成缺少的 GLB 或缩略图；Blender 和 Node.js 在 PATH 中时可以省略对应路径，也支持 `--node`。

每张 `D:\images\icons\star.png` 对应源图旁的 `D:\images\icons\star_thumb.png` 和输出目录中的 `D:\models\icons\star.glb`，不生成 `report.json`。原始 GLB、`.blend` 和诊断文件保留在输出目录的 `.work/` 内。默认不开覆盖：GLB 与缩略图都存在时跳过，仅存在其一时只生成缺少项；指定 `--overwrite` 后重新生成并覆盖两项。批次中单张失败时继续下一张，并以非零退出码结束。输入与输出目录必须分开。

Meshopt 不量化、不减面、不重排、不使用过滤；压缩后验证缓冲区解码逐字节一致、GLB 场景语义一致和 Three.js 加载几何一致。极小模型压缩后可能变大，实际大小记录在输出目录 `.work/` 内的 `compression.json` 中。

## 单独压缩现有 GLB

也可直接对符合本工具输出结构的原始 GLB 运行压缩：

    node glb_tools/compression/compress.mjs D:\models\original.glb D:\models\compressed.glb D:\models\compression-report.json

输出文件必须尚不存在。压缩器目前只支持单个内嵌缓冲区、无贴图与动画、符合当前 Blender 生成布局的静态 GLB；不支持的输入会明确失败，不会宣称任意 GLB 都可无损处理。

## 验证与来源

    .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
    npm test
    .\.venv\Scripts\python.exe tests/verify_pipeline.py --blender D:\Blender\blender.exe

第三条使用合成 PNG 实际运行 Blender，产物保留在 `output/verification/`。来源与公开授权事项见 [docs/provenance.md](docs/provenance.md)。目前未确定复用代码的公开许可证，本仓库尚不具备公开发布条件。
