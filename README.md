# db-mcp-server

给 AI 用的多数据源 MySQL 查询服务，带一个网页配置中心。

解决的问题很具体：**同一个服务在不同环境（开发 / 测试 / 预发 / 生产）连着不同的库**。AI 只有一张 `execute_sql(sql)` 的手牌时，很容易查了测试库却让用户以为在看生产数据。这个项目让 AI 说"查订单服务的测试库"，而不是让它猜一个连接串。

```
用户：查一下订单服务的测试库有多少张表
  ↓
AI 调用 query(sql="...", project="订单服务", env="test")
  ↓
服务端解析 → order-test · 测试 · myapp_order
  ↓
返回结果，并附一行来源标注
```

## 特性

| | |
|---|---|
| **按项目路由** | 项目名或别名（`订单服务` / `order` / `订单`）都能命中，一个项目在每个环境绑一个数据源 |
| **显式指定必须命中** | 说了 `env=prod` 但没绑生产库，直接报错并列出已绑定的环境，**不会降级**到别的库返回数据 |
| **只读保护** | 标记为只读的数据源（通常是生产），`execute` 在服务端被拒绝，不依赖 AI 自觉 |
| **配置热加载** | 配置中心改完数据源，MCP 下次调用就生效，不用重启客户端 |
| **执行记录** | 每次查询落 `query_log.jsonl`，界面可分页查看：时间、来源、SQL、耗时、成功/失败、失败原因 |
| **无库模式** | `database` 可以留空，用 `库名.表名` 跨库查询 |
| **单文件前端** | 零构建、零框架，改样式只需编辑 `webui/index.html` |

## 界面

启动后打开 `http://127.0.0.1:8765`：

- 上半部分：**数据源**（按环境分 tab）与**项目映射**左右并列，各有常驻的新增入口
- 下半部分：**执行记录**，左侧查询流水，右侧成功/失败/平均耗时统计

只有 `127.0.0.1` 监听，没有登录鉴权，靠回环地址隔离。

## 快速开始

### 1. 安装依赖

```bash
python -m pip install -r requirements.txt
```

需要 Python 3.10+（用到了 `str | None` 类型标注）。依赖：`mcp`、`mysql-connector-python`、`python-dotenv`、`fastapi`、`uvicorn`。

### 2. 启动配置中心

```bash
python webui_server.py
```

或双击 `start_webui.bat`（Windows）。端口从 8765 起，被占用就自动顺延，启动时会打印实际地址。

首次运行若 `config.json` 不存在，会从 `.env` 尝试迁移一份；没有 `.env` 就用默认配置（dev / test / staging / prod 四个环境）。

### 3. 配置数据源和项目

**新增数据源** —— 填主机、端口、账号。库名那栏点「加载库列表」可以真实连库列出所有可访问的库，不用手打。**库名留空表示不限定库**。

**新增项目** —— 项目名 + 别名，然后给每个环境挑一个数据源。绑定规则：

- 只绑 1 个环境 → 这个环境自动成为默认，不需要配 `defaultEnv`
- 绑多个环境 → 必须指定默认环境，下拉只列出已绑定的环境
- 一个都不绑 → 保存被拦截

**设默认数据源** —— 数据源行首的星标。AI 完全不指定目标时用它。

### 4. 接入 MCP 客户端

```json
{
  "mcpServers": {
    "db-mcp": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/db_mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

三个必须注意的点：

1. **`command` 写绝对路径**。如果 `python` 是另一个未装依赖的解释器（比如被其他工具托管的版本），MCP 进程会启动失败，客户端通常只显示"未连接"，不告诉你原因。
2. **`args` 也写绝对路径**。客户端拉起子进程时的 cwd 不一定是项目目录。
3. **`PYTHONIOENCODING=utf-8`**。Windows 下 Python 的 stdout 默认 GBK，而 MCP 走 UTF-8；错误信息里有中文时会出现乱码或 JSON-RPC 解析失败。

不同客户端放配置的位置：

| 客户端 | 位置 |
|---|---|
| Qoder | `~/.qoder/settings.json` → `mcpServers`，或项目根 `.mcp.json` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| WorkBuddy | `~/.workbuddy/mcp.json` |

新增或删除工具后**必须重启客户端**（工具列表会被缓存）；只改数据源不用。

## 工具清单

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `query` | 执行 SELECT，返回 JSON 结果集 | `sql`、`params`、`project` / `env` / `datasource` |
| `execute` | 执行 INSERT / UPDATE / DELETE | 同上，只读数据源上会被拒绝 |
| `list_tables` | 列出目标库的表名 | `project` / `env` / `datasource` |
| `describe_table` | 查看表结构（列名、类型、主键） | 另加 `table` |
| `get_schema` | 完整结构（所有表及其字段） | 表多时结果很大，优先用 `describe_table` |
| `search_data` | 在所有文本字段中模糊搜索 | 另加 `table`、`keyword`、`limit` |
| `list_projects` | 列出项目、别名、各环境绑定 | — |
| `list_datasources` | 列出数据源，不含密码 | — |

`query` / `execute` 的返回里会附一行来源标注：

```
[
  { "db": "myapp_order", "host": "db-test.internal" }
]

-- 来源：order-test · 测试 · myapp_order
```

这行不是装饰。没有它，AI 查了哪个库只能靠猜。

## 目标解析优先级

```
1. datasource 显式指定            → 直接取该数据源
2. project + env                  → project.bindings[env]
   ├─ env 不在环境列表里           → 报错，列出可用 key
   └─ 项目未绑定该环境             → 报错，列出已绑定的环境
3. project 无 env                 → 绑 1 个就用它 / 绑多个用 defaultEnv
4. 都没传                         → 默认数据源 → 默认项目
5. 仍解析不出                     → 报错，列出可用项目与数据源
```

第 2 条是刻意设计成"报错"的。原方案里写的是"项目没绑该环境就降级到默认环境"，改成报错的原因：

```
用户：查一下订单服务的生产数据
AI：query(project="订单服务", env="prod")
降级：订单服务没绑 prod → 用测试库 → 返回测试数据
用户看着屏幕上的数字，以为那是生产环境的
```

静默给错东西比直接报错危险得多。

## CLI

同一个脚本带子命令就是命令行工具，适合调试：

```bash
python db_mcp_server.py list-projects
python db_mcp_server.py list-datasources
python db_mcp_server.py query --project 订单服务 --env test --sql "SELECT 1"
python db_mcp_server.py query --sql "SHOW DATABASES"          # 走默认数据源
python db_mcp_server.py describe-table --datasource order-test --table db.tbl
```

结果 JSON 走 stdout，`-- 来源：xxx` 走 stderr，方便脚本管道处理。CLI 调用同样会写执行记录。

## 配置

`config.json`（不提交，见 `.gitignore`）：

```json
{
  "version": 7,
  "environments": [
    { "key": "test", "label": "测试", "order": 2, "readonlyHint": false },
    { "key": "prod", "label": "生产", "order": 4, "readonlyHint": true }
  ],
  "global": {
    "defaultDatasource": "ds_order_test",
    "defaultProject": null,
    "defaultEnv": null
  },
  "datasources": [
    {
      "id": "ds_order_test",
      "alias": "order-test",
      "env": "test",
      "driver": "mysql",
      "host": "10.0.0.1",
      "port": 3306,
      "user": "readonly_user",
      "password": "${ORDER_TEST_PASSWORD}",
      "database": "",
      "readonly": false
    }
  ],
  "projects": [
    {
      "id": "p_order",
      "name": "订单服务",
      "aliases": ["order", "订单"],
      "defaultEnv": "test",
      "bindings": { "test": "ds_order_test" }
    }
  ]
}
```

从零开始可以照抄 `config.example.json`。

**密码两种写法**：

- 明文 `"password": "secret"` —— 简单，适合内网自用
- 变量引用 `"password": "${ORDER_TEST_PASSWORD}"` —— 从环境变量取，配置文件可以安全备份和分享

界面上密码始终显示为 `******`，编辑时留空即保持原值。

**环境变量**：

| 变量 | 作用 |
|---|---|
| `DB_MCP_CONFIG` | 指向另一个配置文件，测试时用，避免污染真实配置 |
| `DB_MCP_LOG` | 指向另一个执行记录文件 |

## 目录结构

```
config_store.py     配置层：原子写、mtime 热加载、密码解析、解析器、乐观锁
db_mcp_server.py    MCP 服务（stdio）+ CLI，8 个工具
webui_server.py     FastAPI 后端，17 个路由
query_log.py        执行记录：JSONL 追加、5MB 轮转、分页、统计
migrate_env.py      .env → config.json，幂等
webui/index.html    配置界面，单文件零构建
```

**一条硬约束**：`config_store.py` 的 `resolve()` 是 MCP 服务和 Web 界面共用的唯一解析入口。两边各写一套的话，会出现"界面显示默认是测试库、AI 实际查了生产库"这种无法排查的问题。

## 安全

- **只监听 `127.0.0.1`**，没有登录鉴权。不要改成 `0.0.0.0`——界面上存着数据库凭据
- **`config.json`、`.env`、`query_log.jsonl` 已加入 `.gitignore`**。执行记录里有完整 SQL，泄露的业务信息比密码更多
- **只读拦截在服务端**。AI 看不到密码，也绕不过 `readonly` 检查
- 生产库建议：`readonly: true` + 只读账号 + 密码用变量引用，三重

## 已知限制

| 限制 | 说明 |
|---|---|
| 只支持 MySQL | `driver` 字段已预留，接 PostgreSQL 需要给 `list_tables` / `describe_table` 按方言分支 |
| 无登录鉴权 | 靠回环地址隔离 |
| `list_tables` / `get_schema` 在无库数据源上不可用 | `SHOW TABLES` 必须有当前库，会给出提示改用 `库名.表名` |
| `get_schema` 不缓存 | 表多的库较慢，优先用 `describe_table` |
| 一个项目每环境只能绑一个数据源 | 需要"一个环境查两个库"时用无库数据源 + 全限定名 |

## License

MIT
