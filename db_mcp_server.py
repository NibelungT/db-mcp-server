# -*- coding: utf-8 -*-
"""DB MCP Server —— 多数据源 + 项目路由。

两种启动方式：
  无参数        走 MCP stdio 服务（由 MCP 客户端拉起）
  带子命令      走 CLI（输出纯 JSON，便于脚本消费）

目标库通过 project / env / datasource 三个可选参数定位，
解析逻辑统一在 config_store.ConfigStore.resolve，与配置界面共用同一份规则。
config.json 不存在时回退到 .env 单库模式，保证旧客户端配置不失效。
"""
import asyncio
import json
import os
import sys
import time

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

import query_log
from config_store import BASE_DIR, ConfigError, ConfigStore, resolve_password
from db_dialect import DialectError, get_dialect, normalize_driver

app = Server("db-mcp-server")
store = ConfigStore()

TARGET_PROPS = {
    "project": {
        "type": "string",
        "description": "项目名或别名，如「订单服务」「order」。不确定有哪些项目时先调 list_projects；用户没说项目名时留空",
    },
    "env": {
        "type": "string",
        "description": "环境 key，如 dev / test / prod。可用值见 list_projects；用户没说环境时留空",
    },
    "datasource": {
        "type": "string",
        "description": (
            "直接指定数据源别名，如 order-test。"
            "**仅当用户明确说出库名或数据源名时才传**；"
            "用户没指定时留空，系统会使用默认数据源，不要自己从列表里挑一个"
        ),
    },
}


def schema(extra=None, required=None):
    props = dict(TARGET_PROPS)
    if extra:
        props.update(extra)
    return {"type": "object", "properties": props, "required": required or []}


# ------------------------------------------------------------------ 目标解析

def legacy_datasource():
    """.env 单库模式，仅在 config.json 不存在时使用。"""
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    return {
        "alias": ".env 单库",
        "env": None,
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", 3306)),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "").strip(),
        "readonly": False,
        "params": {},
    }


def resolve_target(args):
    """返回 (datasource, 实际 env, 解析方式)。配置不存在时回退 .env。"""
    if not os.path.exists(store.path):
        return legacy_datasource(), None, ".env 兜底"
    return store.resolve(
        project=args.get("project"),
        env=args.get("env"),
        datasource=args.get("datasource"),
    )


def source_label(ds, env):
    parts = [ds.get("alias") or "?"]
    if env:
        try:
            data = store.load()
            label = next((e["label"] for e in data["environments"] if e["key"] == env), env)
            parts.append(label)
        except Exception:
            parts.append(env)
    db = ds.get("database") or ""
    schema = (ds.get("schema") or "").strip()
    if schema and normalize_driver(ds.get("driver")) == "postgres":
        # PG 的库名和 schema 都要露出来，否则不知道查的是哪个命名空间
        db = f"{db}/{schema}" if db else schema
    if db:
        parts.append(db)
    return " · ".join(parts)


def open_conn(ds):
    """按驱动的方言层建连，返回 (dialect, connection)。

    密码在这一层才解析：配置里存的可能是 ${VAR} 引用，
    解析结果不落盘、也不进日志。
    """
    d = get_dialect(ds.get("driver"))
    patched = dict(ds)
    patched["password"] = resolve_password(ds.get("password", ""))
    return d, d.connect(patched)


def text(s):
    return [types.TextContent(type="text", text=s)]


def payload(data, ds, env):
    """JSON 在前便于程序解析，来源标注在后便于人和 AI 确认查的是哪个库。"""
    body = json.dumps(data, ensure_ascii=False, default=str, indent=2)
    return text(f"{body}\n\n-- 来源：{source_label(ds, env)}")


# ------------------------------------------------------------------- 工具定义

@app.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="query",
            description=(
                "执行 SELECT 查询，返回 JSON 结果集。"
                "用 project / env / datasource 指定目标库；"
                "不确定有哪些项目可查时，先调用 list_projects。"
                "不传任何定位参数时使用全局默认数据源。"
            ),
            inputSchema=schema(
                {"sql": {"type": "string", "description": "SELECT 语句"},
                 "params": {"type": "array", "items": {}, "description": "可选，参数化占位符的值"}},
                ["sql"],
            ),
        ),
        types.Tool(
            name="execute",
            description=(
                "执行 INSERT / UPDATE / DELETE，返回影响行数。"
                "对标记为只读的数据源（通常是生产库）会被直接拒绝。"
            ),
            inputSchema=schema(
                {"sql": {"type": "string"},
                 "params": {"type": "array", "items": {}}},
                ["sql"],
            ),
        ),
        types.Tool(
            name="list_tables",
            description="列出目标库的所有表名",
            inputSchema=schema(),
        ),
        types.Tool(
            name="describe_table",
            description="查看目标库中某张表的字段结构（列名、类型、主键等）",
            inputSchema=schema(
                {"table": {"type": "string", "description": (
                    "表名。可写成「库名.表名」：MySQL 下第一段是 database，"
                    "PostgreSQL 下第一段是 schema（默认 public）"
                )}},
                ["table"],
            ),
        ),
        types.Tool(
            name="get_schema",
            description="获取目标库的完整结构（所有表及其字段）。表很多时结果较大，优先用 describe_table",
            inputSchema=schema(),
        ),
        types.Tool(
            name="search_data",
            description="在指定表的所有文本字段中模糊搜索关键词",
            inputSchema=schema(
                {"table": {"type": "string", "description": (
                    "表名，可写成「库名.表名」；PostgreSQL 下第一段是 schema"
                )},
                 "keyword": {"type": "string"},
                 "limit": {"type": "integer", "default": 20}},
                ["table", "keyword"],
            ),
        ),
        types.Tool(
            name="list_projects",
            description=(
                "列出所有已配置的项目、它们的别名、默认环境，以及各环境绑定的数据源。"
                "查库前如果不确定用哪个项目名，先调用这个。"
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_datasources",
            description="列出所有数据源（环境、地址、库名、是否只读），不含密码",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ------------------------------------------------------------------- 工具实现

def describe_call(name, args):
    """给没有 sql 参数的工具生成一条可读的等价语句，用于日志展示。"""
    if args.get("sql"):
        return args["sql"]
    key = str(name).replace("-", "_")
    if key == "list_tables":
        return "SHOW TABLES"
    if key == "describe_table":
        return f"DESCRIBE `{args.get('table', '?')}`"
    if key == "get_schema":
        return "SHOW TABLES + DESCRIBE（全部表）"
    if key == "search_data":
        return f"SELECT * FROM `{args.get('table', '?')}` WHERE <文本字段> LIKE '%{args.get('keyword', '')}%'"
    return str(name)


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "list_projects":
        return payload(store.list_projects(), {"alias": "配置"}, None)
    if name == "list_datasources":
        return payload(store.list_datasources(), {"alias": "配置"}, None)

    start = time.time()
    ds, env, how, project = None, None, None, arguments.get("project")
    sql = describe_call(name, arguments)

    def log(ok, rows=None, error=None):
        query_log.record(
            name, ds, env, sql, ok=ok, rows=rows,
            ms=int((time.time() - start) * 1000),
            error=error, project=project, params=arguments.get("params"),
            resolved_by=how,
        )

    try:
        ds, env, how = resolve_target(arguments)

        if name == "execute" and ds.get("readonly"):
            log(False, error="只读数据源拒绝")
            return text(
                f"[拒绝] 数据源「{ds.get('alias')}」标记为只读"
                f"（{source_label(ds, env)}），execute 已被拦截。\n"
                f"如需写入，请在配置界面关闭该数据源的只读开关。"
            )

        handler = {
            "query": _query,
            "execute": _execute,
            "list_tables": _list_tables,
            "describe_table": _describe_table,
            "get_schema": _get_schema,
            "search_data": _search_data,
        }.get(name)
        if not handler:
            return text(f"[错误] 未知工具：{name}")

        out = handler(ds, env, arguments)
        log(True, rows=out.get("rows"))
        return out["content"]

    except ConfigError as e:
        log(False, error=str(e))
        return text(f"[无法定位数据源] {e}")
    except DialectError as e:
        log(False, error=str(e))
        return text(f"[提示] {e}")
    except Exception as e:
        log(False, error=f"{type(e).__name__}: {e}")
        return text(f"[错误] {type(e).__name__}: {e}")


def _query(ds, env, args):
    d, conn = open_conn(ds)
    try:
        cur = d.cursor(conn)
        cur.execute(args["sql"], args.get("params") or [])
        rows = cur.fetchall()
        cur.close()
        return {"content": payload(rows, ds, env), "rows": len(rows)}
    finally:
        conn.close()


def _execute(ds, env, args):
    d, conn = open_conn(ds)
    try:
        cur = conn.cursor()
        cur.execute(args["sql"], args.get("params") or [])
        conn.commit()
        affected = cur.rowcount
        last_id = d.insert_id(cur)
        cur.close()
        id_part = f"，insertId: {last_id}" if last_id else ""
        return {
            "content": text(
                f"影响行数: {affected}{id_part}\n"
                f"-- 来源：{source_label(ds, env)}"
            ),
            "rows": affected if affected and affected > 0 else 0,
        }
    finally:
        conn.close()


def _list_tables(ds, env, args):
    d, conn = open_conn(ds)
    try:
        tables = d.list_tables(conn, ds)
        return {"content": payload(tables, ds, env), "rows": len(tables)}
    finally:
        conn.close()


def _describe_table(ds, env, args):
    d, conn = open_conn(ds)
    try:
        fields = d.columns(conn, ds, args["table"])
        return {"content": payload(fields, ds, env), "rows": len(fields)}
    finally:
        conn.close()


def _get_schema(ds, env, args):
    d, conn = open_conn(ds)
    try:
        tables = d.list_tables(conn, ds)
        result = {}
        for t in tables:
            result[t] = d.columns(conn, ds, t)
        return {"content": payload(result, ds, env), "rows": len(result)}
    finally:
        conn.close()


def _search_data(ds, env, args):
    d, conn = open_conn(ds)
    try:
        table = args["table"]
        keyword = args["keyword"]
        limit = int(args.get("limit", 20))
        cols = [
            c["name"] for c in d.columns(conn, ds, table)
            if "char" in str(c["type"]).lower() or "text" in str(c["type"]).lower()
        ]
        if not cols:
            return {"content": text(f"表「{table}」没有可搜索的文本字段"), "rows": 0}

        where = " OR ".join(f"{d.quote(c)} {d.like_operator} %s" for c in cols)
        sql = f"SELECT * FROM {d.ref(ds, table)} WHERE {where} LIMIT {limit}"
        cur = d.cursor(conn)
        cur.execute(sql, [f"%{keyword}%"] * len(cols))
        rows = cur.fetchall()
        cur.close()
        return {"content": payload(rows, ds, env), "rows": len(rows)}
    finally:
        conn.close()


# --------------------------------------------------------------------- 启动

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def cli_mode():
    import argparse

    parser = argparse.ArgumentParser(description="DB MCP Server CLI")
    sub = parser.add_subparsers(dest="command")

    def add_target(p):
        p.add_argument("--project", help="项目名或别名")
        p.add_argument("--env", help="环境 key")
        p.add_argument("--datasource", help="数据源别名")

    p_q = sub.add_parser("query", help="执行 SELECT")
    p_q.add_argument("--sql", required=True)
    add_target(p_q)

    p_e = sub.add_parser("execute", help="执行 INSERT/UPDATE/DELETE")
    p_e.add_argument("--sql", required=True)
    add_target(p_e)

    p_l = sub.add_parser("list-tables", help="列出所有表")
    add_target(p_l)

    p_d = sub.add_parser("describe-table", help="查看表结构")
    p_d.add_argument("--table", required=True)
    add_target(p_d)

    p_s = sub.add_parser("get-schema", help="获取完整结构")
    add_target(p_s)

    sub.add_parser("list-projects", help="列出项目映射")
    sub.add_parser("list-datasources", help="列出数据源")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    started = time.time()
    ds, env, how = None, None, None
    try:
        if args.command == "list-projects":
            print(json.dumps(store.list_projects(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "list-datasources":
            print(json.dumps(store.list_datasources(), ensure_ascii=False, indent=2))
            return 0

        target = {
            "project": getattr(args, "project", None),
            "env": getattr(args, "env", None),
            "datasource": getattr(args, "datasource", None),
        }
        ds, env, how = resolve_target(target)
        shown_sql = describe_call(args.command, {"sql": getattr(args, "sql", None),
                                                 "table": getattr(args, "table", None)})

        if args.command == "execute" and ds.get("readonly"):
            query_log.record(
                args.command, ds, env, shown_sql, ok=False,
                ms=int((time.time() - started) * 1000),
                error="只读数据源拒绝", project=getattr(args, "project", None),
                resolved_by=how,
            )
            print(json.dumps(
                {"error": f"数据源「{ds.get('alias')}」只读，execute 被拒绝"},
                ensure_ascii=False,
            ), file=sys.stderr)
            return 1

        started = time.time()
        if args.command == "query":
            res = _query(ds, env, {"sql": args.sql})
        elif args.command == "execute":
            res = _execute(ds, env, {"sql": args.sql})
        elif args.command == "list-tables":
            res = _list_tables(ds, env, {})
        elif args.command == "describe-table":
            res = _describe_table(ds, env, {"table": args.table})
        elif args.command == "get-schema":
            res = _get_schema(ds, env, {})
        else:
            parser.print_help()
            return 1

        query_log.record(
            args.command, ds, env,
            shown_sql,
            ok=True, rows=res.get("rows"),
            ms=int((time.time() - started) * 1000),
            project=getattr(args, "project", None),
            resolved_by=how,
        )

        out = res["content"][0].text
        if "\n\n-- 来源：" in out:
            body, note = out.rsplit("\n\n-- 来源：", 1)
            print(body)
            print(f"-- 来源：{note}", file=sys.stderr)
        else:
            print(out)
        return 0

    except ConfigError as e:
        query_log.record(
            getattr(args, "command", "?"), None, None,
            describe_call(getattr(args, "command", "?"),
                          {"sql": getattr(args, "sql", None),
                           "table": getattr(args, "table", None)}),
            ok=False, ms=int((time.time() - started) * 1000),
            error=str(e), project=getattr(args, "project", None),
        )
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as e:
        # SQL 语法错误、连不上库等：也要留痕，否则日志里看不出「跑过但炸了」
        query_log.record(
            getattr(args, "command", "?"), ds, env,
            describe_call(getattr(args, "command", "?"),
                          {"sql": getattr(args, "sql", None),
                           "table": getattr(args, "table", None)}),
            ok=False, ms=int((time.time() - started) * 1000),
            error=f"{type(e).__name__}: {e}"[:300],
            project=getattr(args, "project", None),
        )
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli_mode())
    asyncio.run(main())
