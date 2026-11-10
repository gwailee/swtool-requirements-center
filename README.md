# SWTool 需求中心 · 配合 CLI 的 AI 编码协作

A self-hosted, open source issue tracker and requirements management workspace backed by PostgreSQL. Pair it with an AI coding agent through SWTool CLI, with GPT, Claude or custom OpenAI-compatible models available through your configured SWTool provider.

独立部署的需求与工单协作服务，使用 Python 与 PostgreSQL，提供账号审核、回复、附件、跨中心互联、管理员排序和父子工单关系。支持黑色、白色、高对比度主题；界面资源随源码分发。

2026 上海商江网络科技有限公司版权所有。开发者：软工云（swtool.com）开发团队。项目联系：uqtool@163.com。
项目自有及获得授权的源码采用 [Apache License 2.0](LICENSE)；第三方组件保留各自许可，见 [NOTICE](NOTICE) 与 [SOURCE_NOTES.md](SOURCE_NOTES.md)。

## 为什么使用

- **让工单驱动编码协作**：从需求、父子任务与验收条件开始，衔接 CLI 输出、人工交互、测试结果和 Git 版本记录。
- **灵活连接模型**：通过 SWTool CLI 配置可用的 GPT、Claude 或自定义 OpenAI 兼容模型，在自己的项目目录开展 AI coding。
- **多中心协作**：使用个人 Key 连接不同需求中心，目标服务继续校验读取、写入与管理员权限。
- **自主部署和数据控制**：使用自己的 PostgreSQL、附件目录和 HTTPS 域名，源码开放，支持离线页面资源。

## 配合 SWTool CLI 使用

把需求、验收条件与父子任务整理在需求中心，再在开发机或 SSH 项目终端运行 SWTool CLI，让编码 Agent 基于工单内容协助实现。查看 CLI 输出与交互、验证改动并提交版本后，将过程、验证结果及提交编号写回工单，最后由有权限的人员确认并关闭。

模型由**执行主机上的 SWTool CLI provider 配置**决定：可使用该 provider 实际提供的 GPT、Claude 或自定义 OpenAI 兼容模型，不能保证任意供应商或型号都可用。密钥保存在 CLI 的用户配置中。独立需求中心服务端的数据库与个人 Key 配置用于工单存储和 API 鉴权，不是模型供应商配置；网页工单驱动把任务交给执行主机上的 SWTool CLI，并展示日志与交互；本机执行需显式启用，SSH 执行需配置目标指纹、账号和目录授权。本仓库包含网页与驱动接口，但不包含主项目的受控 runner 部署资产；执行主机需另行配置隔离 runner、CLI 与模型。仅克隆源码或打开开关不足以启动编码任务。

在已安装并完成配置的 SWTool CLI 中，先列出可用模型，再使用实际模型 ID：

```sh
swtool models swtool
swtool run --model 'swtool/<MODEL_ID>' '按工单验收条件分析并实现，完成后报告验证结果'
```

`<MODEL_ID>` 必须替换为列出的模型；自定义 provider 的模型路径以该 provider 配置为准。工单驱动只调用 `tool=swtool`，不直接调用 Claude CLI；网页启动请求不选择 provider/model，所用模型来自执行主机 CLI 配置。执行目录和 SSH 操作取决于账号及执行主机授权，勿把有权限的工单账号等同于无限制主机权限。

也可通过下方工单 API 与已有 CLI / 自动化集成；SWTool CLI 的安装与平台发行信息请查看 [SWTool CLI 指南](https://swtool.com/swtool_cli_guide.html)。CLI 本体不包含在本仓库源码 Release 中。

## 环境与安装

运行要求 Python 3.11+、PostgreSQL 14+。部署说明以 Linux、Bash、systemd 为准；其他操作系统尚未完成等效部署验证。GitHub Release 提供源码 ZIP 及 SHA-256 校验文件，不提供 Windows EXE、签名安装器或预编译服务包。

```sh
git clone https://github.com/gwailee/swtool-requirements-center.git
cd swtool-requirements-center
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp service.env.example service.env
chmod 600 service.env
```

使用数据库管理员创建专用数据库和账号，替换下面的密码占位符：

```sql
CREATE ROLE requirements LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';
CREATE DATABASE requirements OWNER requirements;
```

编辑 `service.env`，密码中的 URL 保留字符须百分号编码：

```sh
SWTOOL_DATABASE_URL='postgresql://requirements:REPLACE_WITH_URL_ENCODED_PASSWORD@127.0.0.1:5432/requirements'
SWTOOL_HOST=127.0.0.1
SWTOOL_PORT=8080
# 可选；默认是当前源码目录下的 data
# SWTOOL_DATA_DIR=/srv/requirements-center/data
```

```sh
bash start.sh
```

打开 `http://127.0.0.1:8080/`。首次启动自动初始化数据库结构并生成持久主密钥 `data/.secret`。**在仅管理员可访问的初始化阶段完成第一个账号注册**：首个账号成为管理员，后续注册账号需要审核。唯一受支持的 ASGI 入口为 `standalone:app`；不得使用共享实现 `server.app:app` 启动整站。

必须使用独立数据库、数据目录和密钥。不要指向其他 SWTool 实例的数据库或复制其运行配置。

### 可选工单驱动

完成普通工单服务部署后，可在网页配置执行目标。本机模式默认关闭，管理员只有在已正确部署隔离 runner 时才设置 `SWTOOL_REQUIREMENTS_LOCAL_DRIVER_ENABLED=1` 并重启服务。runner 及其父目录需要满足服务端的所有者和权限校验，执行配置需限制可用项目目录；不要用任意 shell 脚本替代。

SSH 模式需要预先配置目标账号、可信主机指纹、目录授权和受控 runner。确认指纹应通过独立可信渠道；不要为绕过错误而关闭校验。执行主机的 SWTool CLI 负责模型认证，网页记录任务、输出和交互。驱动 API 可达不表示执行主机已具备完整运行环境。

## HTTPS 与常驻服务

使用非 root 专用账号部署，例如 `/srv/requirements-center`。调整 `requirements-center.service` 的账号和路径，确保已安装虚拟环境依赖，再安装服务：

```sh
sudo cp requirements-center.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now requirements-center
sudo systemctl status requirements-center
```

通过 HTTPS 反向代理对外开放，应用默认仅监听回环地址。Nginx 的代理位置至少包含下列设置；证书、域名和请求大小按实际部署配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
}
```

代理必须覆盖客户端传入的 `X-Real-IP`；应用只信任来自回环代理的该头，用于下载审计。不要在公网明文传输密码、会话或个人 Key。systemd 重启不会自动安装依赖。

### 运行检查

当前独立入口不提供专门的 `/healthz` 或 `/readyz`。检查服务状态及静态入口，再用有效个人 Key 调用身份接口验证数据库与鉴权链路：

```sh
curl --fail http://127.0.0.1:8080/ -o /dev/null
# 身份接口用法见下方 API 示例
```

匿名身份接口返回 401 是鉴权行为，不能替代已登录的数据库检查。检查失败时查看本机服务日志，公开报告前先移除敏感内容。

## 使用与权限

管理员在“账号管理”审核账号；普通用户只能按服务端授权处理自己的工单。管理员可管理中心内工单。回复可通过“回复”按钮引用；附件与正文遵循相同可见性约束。

管理员可拖动工单标题排序；键盘聚焦列表条目后使用 Alt+上下方向键；手机先选择工单，再使用列表外的“上移/下移”按钮。只读 Key 不显示写入入口，排序不改变更新时间。

创建工单时可按需搜索并选择同中心父工单，默认无父工单；父关系仅能在创建时指定。详情显示父→当前→直属子关系，节点可点击切换，子项超过 200 条时明确提示截断。手机采用纵向布局，长标题换行，按钮至少 44px，避免页面横向溢出。不可读关系不会显示标题或内容。

登录后“需求驱动”可生成个人 Key、连接其他中心；目标中心继续按账号权限与 Key scope 鉴权。互联要求可公开访问的 HTTP(S) 地址，拒绝私网目标，正式使用 HTTPS。轮换 Key 后需更新另一中心的连接。远端附件能力取决于连接方式，受限时在目标中心操作。

## API

以下示例变量均为占位值，请在自己的终端设置；不要把真实 Key 放进工单、仓库或截图。完整版本路径是 `/api/v1/api/tickets`，**不是** `/api/v1/tickets`；也兼容直接 `/api/tickets`。

```sh
BASE_URL='https://requirements.example.com'
PERSONAL_KEY='REPLACE_WITH_PERSONAL_KEY'
TICKET_ID='REPLACE_WITH_TICKET_ID'
curl --fail "$BASE_URL/api/v1/api/requirements/identity" \
  -H "Authorization: Bearer $PERSONAL_KEY"
curl --fail "$BASE_URL/api/v1/api/tickets?q=example&page_size=20" \
  -H "Authorization: Bearer $PERSONAL_KEY"
curl --fail -X POST "$BASE_URL/api/v1/api/tickets" \
  -H "Authorization: Bearer $PERSONAL_KEY" -H 'Content-Type: application/json' \
  -d '{"title":"Example request","description":"Reproduction and expected result","parent_ticket_id":null}'
curl --fail -X POST "$BASE_URL/api/v1/api/tickets/$TICKET_ID/comments" \
  -H "Authorization: Bearer $PERSONAL_KEY" -H 'Content-Type: application/json' \
  -d '{"body":"Progress update"}'
```

| 方法 | 完整路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/api/requirements/identity` | 当前身份、scope、capabilities |
| GET / POST | `/api/v1/api/tickets` | 分页搜索 / 创建 |
| GET / PATCH / DELETE | `/api/v1/api/tickets/{id}` | 详情 / 编辑 / 删除 |
| POST | `/api/v1/api/tickets/{id}/comments` | JSON `body` 回复 |
| POST | `/api/v1/api/tickets/{id}/attachments?comment_id=…` | multipart `file` 上传 |
| GET | `/api/v1/api/tickets/{id}/attachments/{attachment_id}` | 下载附件 |

`tickets:read` 允许读取授权可见的工单；`tickets:write` 才允许写入，仍不授予管理员权限。401 检查 Key 和账号状态，403 检查 scope 与角色，404 检查完整路径、目标可见性及中心。Bearer Key 请求不要求浏览器 Cookie 或 CSRF；本机浏览器登录会话沿用自己的安全约束。

创建的 `parent_ticket_id` 可省略或为 `null`、空字符串，非空最长 128 字符；父票须同中心且可读。详情包含 `parent_ticket`、`child_tickets` 的 `{id,title,status}` 摘要和 `children_truncated`。PATCH 不能改变父关系。管理员排序 PATCH 只传 `{"move_before":"TARGET_ID"}` 或 `{"move_after":"TARGET_ID"}`，不能混入其他字段或跨中心。状态 `closed` 兼容映射为 `resolved`。

Key 创建、轮换、撤销、连接管理和管理员全站归档仅支持本机登录会话。`GET /issues/export/zip` 与 `POST /issues/import/zip` 是用户数据归档，个人 Key 无权调用；与源码 ZIP 不同。导入需要明确的 `identity_map`，不会按同名自动绑定账号或覆盖重复工单。

## 备份、恢复与升级

同时备份 PostgreSQL（例如 `pg_dump` 自定义格式）、完整数据目录（含附件、插件状态、`.secret`）和受保护的 `service.env`。将备份加密并限制访问，在独立环境定期验证恢复。`.secret` 丢失会影响已存连接凭据，不能随升级重建。

升级前保存当前源码版本并完成一致备份；阅读 [CHANGELOG.md](CHANGELOG.md)，停止本实例写入后替换源码，保留环境与数据目录，安装锁定依赖，再启动 `standalone:app` 并验证登录、读取和附件。数据库结构可能在启动时更新：旧源码不能保证兼容新结构，回退必须连同匹配的数据库与数据备份恢复。不要把真实配置、数据库或附件加入源码 Release。

独立版插件管理只接受本发行可核验的需求中心包，不执行任意上传 Python；不支持任意第三方动态插件。共享 schema 保留部分未启用模块表，不表示那些服务已启用。

## 参与与安全

开发环境、测试入口和 PR 检查见 [CONTRIBUTING.md](CONTRIBUTING.md)。请通过 Issue 报告普通问题；漏洞请遵循 [SECURITY.md](SECURITY.md) 私下报告。安全公开导出仓库的源码发布和校验流程见 `.github/workflows/release.yml`；其校验脚本要求公开导出后的完整清单及 `public_metadata`，不能直接套用于主仓原始 standalone 清单。主仓在线源码归档由 `export_source.py` 按原始清单生成。1.4.11 因 workflow dispatch 权限受限，使用官方 Release API 发布，不代表该 workflow 或 CI 已通过；下载后应核对随附 SHA-256。

归档兼容性：全站 CSV 归档支持 API 创建的待审核（pending）工单及其状态历史；恢复仍要求显式账号映射，并校验未知状态、关联、重复 ID 与附件完整性。


## 回收站保留与清理（1.4.9）

撤回工单默认保留 7 天，清理任务每小时运行，每批最多删除 100 条。`SWTOOL_REQUIREMENTS_TRASH_RETENTION_DAYS` 可设置正整数天数；非法值停止本轮清理，不能降为立即删除。恢复后再次撤回从新撤回时间重新计时。

缺失、非法或未来撤回时间不自动删除；独立版 PostgreSQL 中原始时间为 NULL 而仅在展示层生成的合成时间也不作为删除依据。旧数据不补造撤回时间，管理员应确认后手动处理。插件禁用时停止相关处理，附件遵循既有清理策略。任务只记录汇总数量与通用失败状态，不输出工单、中心、用户标识或内容。不需要 schema 变更。


## 详情首屏响应（1.4.10）

点击列表标题后，详情面板先同步显示当前授权列表中的真实标题与正文，只读展示，不以占位文本替代内容。完整详情请求在后台补齐权限、回复、附件与历史；列表数据不授权编辑或其它写操作，完整详情仍以服务端校验结果为准。权限失败时清除预览；快速切换通过请求代次隔离过期响应，后台详情不覆盖用户编辑草稿。

100ms 是点击后真实正文首屏响应的目标，不是对所有设备或线上 P95 的保证。完整详情、网络与 Markdown/图表资源就绪时间应分别测量，不将局部 fixture 等同线上整页性能验收。


## 附件粘贴提示（1.4.11）

新增需求、回复与新增子需求的附件入口统一标注“添加附件（Ctrl+V可直接上传粘贴板截图）”。这是既有图片粘贴能力的提示改进，不改变上传机制或权限。
