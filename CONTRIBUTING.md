# 参与开发

2026 上海商江网络科技有限公司版权所有。维护开发者：软工云（swtool.com）开发团队；项目联系：uqtool@163.com。

提交前请阅读 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)、[SECURITY.md](SECURITY.md) 和 [SOURCE_NOTES.md](SOURCE_NOTES.md)。普通缺陷和建议使用 Issue 模板；漏洞不公开讨论。请提供最小复现、期望行为和验收条件，不提交真实账号、Key、附件或生产日志。

## 本地环境

从本仓库根目录操作，要求 Python 3.11+、PostgreSQL 14+。运行方式见 README。测试必须使用**专用、可清空的测试数据库**，不能指向运行实例；`SWTOOL_TEST_DATABASE_URL` 必须显式配置，缺失应失败而不是跳过数据库验证。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
export SWTOOL_TEST_DATABASE_URL='postgresql://requirements_test:REPLACE_WITH_PASSWORD@127.0.0.1:5432/requirements_test'
.venv/bin/python scripts/verify-source.py
.venv/bin/python -m pytest tests -q
```

测试数据库应预先由管理员创建并归测试账号所有。测试过程仅使用临时数据目录，不复用实际 `service.env` 或 `data/.secret`。CI 在独立 PostgreSQL 服务上运行同样的源码检查和 HTTP 测试；完整主站测试不属于本独立仓库的依赖。

界面改动另请检查手机窄屏、长标题、键盘焦点、只读权限、跨中心异步请求和错误恢复。HTTP 测试通过不等于浏览器视觉验收或所有部署平台均受支持。

## 提交与许可

保持改动集中，新增行为应有相关测试。PR 说明问题、变更后的表现、验证结果和剩余风险；数据库或归档格式变更应说明升级与恢复限制。请勿添加运行数据、密钥、虚拟环境、缓存或编译产物。

只提交你有权按 Apache-2.0 许可贡献的内容；新增第三方代码、字体或资源必须保留原许可，更新 NOTICE 与来源说明。不要给第三方 vendor 文件套项目版权声明。修改分发源文件后，按源码 manifest 的明确条目更新 hash/size，并运行校验；不要扩大为递归扫描整个工作目录。

## 源码发布

本地可运行 `python scripts/build-source.py --output dist/requirements-center-source.zip`，同时生成 `.zip.sha256`；在输出目录执行 `sha256sum --check requirements-center-source.zip.sha256` 验证。

维护者先创建并推送明确的版本标签（例如 `v1.0.0`），再通过 GitHub Actions 的手动 Release 工作流指定该标签发布源码 ZIP 和 SHA-256 文件。工作流重新验证源码和 PostgreSQL HTTP 测试，失败不发布；不会自动创建标签或覆盖已存在的 Release。打包仅包含经过安全校验的 manifest 条目，不包含数据库、附件、实际环境文件或隐藏运行目录。发布前应确认 CI 通过，人工复核版本、许可和变更记录；SHA-256 是一致性校验，不是数字签名。

本仓库不承诺 Windows EXE、签名安装器或预编译服务包。部署示例主要面向 Linux；其他平台需要单独验证。
