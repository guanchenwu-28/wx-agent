# v0.1.1 — 仓库统一安装入口

将安装包、安装入口、说明与源码放在同一个仓库中。

- 根目录 `install.sh` 默认安装 runtime、Skill、依赖并构建原生桥。
- 完整安装说明统一到根目录 `INSTALL.md`。
- `packages/` 直接保存完整安装包、独立 Skill 包与 SHA256 校验文件，Release 同步提供相同文件。
- `scripts/package.py` 用于从 Git 管理的源文件重建安装包，排除包自身，避免递归打包。

运行组件没有改变。仍为 macOS Apple Silicon 源码安装包，测试微信副本与本机数据库访问配置仍需自行准备；安装不自动启动监听。
