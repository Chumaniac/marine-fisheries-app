# 第三方资料与运行时

本仓库自身代码按根目录 LICENSE 授权；第三方资料保留各自许可，不能由根许可证覆盖。

## 独立用户版的运行包

源代码仓库本身不包含依赖可执行文件或用户数据。构建器在组装实际运行包时附带相关许可：

- CPython 3.12.9 与所链接组件的上游许可和元数据保留在 desktop/runtime-licenses/python，组装时复制进运行包。Python 依赖按锁文件安装，保留各自 dist-info 中的许可证和元数据。

- 数据来源与 Inter 字体说明见 third_party/NOTICE.md；OFL 全文保留于 web/fonts。没有再分发训练图像或权重。

模型、数据集、Intiface 应用不在运行包中。借鉴来源和未复制的实现见 docs/references.md。
