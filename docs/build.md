# 构建 澜知海洋 独立程序

macOS 桌面构建需要 macOS 13+ 与 Xcode Command Line Tools。当前实际分发目标是 Apple Silicon；没有声称已提供 Intel 或 Windows 桌面安装包。源码和安装包分开，程序包内含运行文件，私人数据保存在用户目录。

## 编译源码

前提：Python 3.12、uv。前端修改需要 Node 24；外部 JVM/Go 服务是进阶选项。

```sh
uv venv --python 3.12
uv pip sync --python .venv/bin/python requirements.local.lock
```

## 准备运行时

准备一个可重定位的 Python 3.12 运行时副本，例如 uv 的 python-build-standalone 安装。不要复制开发虚拟环境；以下只安装到你创建的运行时副本。

```sh
uv pip sync --python /path/python-copy/bin/python3 --target /path/python-copy/lib/python3.12/site-packages requirements.local.lock
python3 desktop/prepare_runtime.py --output /tmp/new-runtime --python-runtime /path/python-copy
```

构建器保留 CPython、组件与依赖许可，不打包模型、用户数据库、配置或访问文件。

## 打包与验证

```sh
python3 desktop/build_app.py --runtime /tmp/new-runtime --output /tmp/new-apps
```

两个输出目录必须不存在；构建不覆盖旧安装或数据。正式构建要求 Git 干净，运行清单绑定当前提交；本地未提交预览需显式 --preview。

程序包包含「帮助 → 使用指南」，可以离线查看操作步骤和存储位置。复制到新目录后，用最小运行环境启动，检查源码目录不在运行依赖内；桌面与390px窄屏分别核对。测试报告、模型效果和实体设备验收单独记录。

源码检查：

```sh
uv pip sync --python .venv/bin/python requirements.lock
.venv/bin/python -m pytest -q
```

本地构建仅作临时签名；没有 Apple 开发者签名/公证承诺。发布需附运行时版本、源码提交与 SHA-256 校验和，第三方许可随实际运行包一起保留。
