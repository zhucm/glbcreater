# 来源与公开授权

- `glb_tools/conversion/model.py` 从 `cakerdesign/tools/image-to-model.py` 经当前 `png2glb/backend/conversion/model.py` 提取，保留图片建模、Blender 渲染、GLB 重导入和缩略图逻辑，不包含 Caker 素材注册。
- `model-colors.py` 与 `model-curves.py` 来自同一工具目录。来源 SHA-256 记录在同目录 `provenance.json`。
- `glb_tools/compression/compress.mjs` 来自当前 `png2glb` 项目的 Meshopt 单文件压缩实现。
- `model-curves.py` 注释标明参考 Schneider / Graphics Gems 曲线拟合方法，需核对相应引用和复用权利。

未在来源项目根目录发现 LICENSE。复制到本地项目不等于取得公开再授权权利；公开仓库前须由代码权利人确认授权，并核对 Python、Node 和 Blender 的分发声明。本项目不打包 Blender、测试业务图片或原项目数据库。
