# -*- coding: utf-8 -*-
"""数据库方言层。

把 MySQL 与 PostgreSQL 的差异收敛到一个模块：建连、标识符引用、
元数据查询、库/模式枚举。上层（MCP 工具、配置中心、CLI）只调用
这里的统一接口，新增数据库时只需要再加一个 Dialect 子类。

术语差异（重要）：
- MySQL：`库.表` 里的「库」是 database，一个连接绑定一个 database，
  可以跨库查询。
- PostgreSQL：连接绑定的是 database，表实际住在 schema 里。
  所以本模块把 `库.表` 这种写法在 PG 下解释为 `schema.表`，
  PG 不支持跨 database 查询。
"""

from __future__ import annotations


class DialectError(Exception):
    """方言层可预期的错误，上层转成友好提示而不是堆栈。"""


DRIVER_ALIASES = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
}


def normalize_driver(name):
    """把各种写法归一；未知值原样返回，交给校验层报错。"""
    key = str(name or "mysql").strip().lower()
    return DRIVER_ALIASES.get(key, key)


def clean_ident(name):
    """去掉用户可能带上的引号/反引号/空白。"""
    return str(name or "").strip().strip("`\"").strip()


def split_ref(ref):
    """把 `a.b` 拆成 (a, b)；只有一段时返回 (None, b)。"""
    parts = [clean_ident(p) for p in str(ref).split(".")]
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise DialectError(f"表名「{ref}」不合法：最多只能写成「库名.表名」两段")


class Dialect:
    """统一接口。子类只需要实现差异部分。"""

    name = ""
    label = ""
    default_port = 0
    like_operator = "LIKE"
    supports_cross_database = False

    # ---------------------------------------------------------------- 连接
    def connect(self, ds):
        raise NotImplementedError

    def cursor(self, conn):
        raise NotImplementedError

    def placeholder(self):
        return "%s"

    # ------------------------------------------------------------ 标识符
    def quote(self, ident):
        raise NotImplementedError

    def ref(self, ds, table):
        """把用户写的表名渲染成当前驱动下可用的全限定名。"""
        schema, table = split_ref(table)
        if schema:
            return f"{self.quote(schema)}.{self.quote(table)}"
        if self.name == "postgres":
            schema = self.default_schema(ds)
            return f"{self.quote(schema)}.{self.quote(table)}"
        return self.quote(table)

    def default_schema(self, ds):
        raise NotImplementedError

    # -------------------------------------------------------------- 元数据
    def list_tables(self, conn, ds):
        """返回表名列表；无法枚举时抛 DialectError 带说明。"""
        raise NotImplementedError

    def columns(self, conn, ds, table):
        """返回列信息，字段统一为 name/type/nullable/key/default/extra。"""
        raise NotImplementedError

    def databases(self, conn):
        raise NotImplementedError

    def schemas(self, conn):
        return []

    def insert_id(self, cur):
        return None


# --------------------------------------------------------------------- MySQL


class MySQLDialect(Dialect):
    name = "mysql"
    label = "MySQL"
    default_port = 3306
    like_operator = "LIKE"
    supports_cross_database = True

    def connect(self, ds):
        import mysql.connector

        kwargs = dict(
            host=ds["host"],
            port=int(ds.get("port") or self.default_port),
            user=ds.get("user", ""),
            password=ds.get("password", ""),
            connection_timeout=10,
        )
        if ds.get("database"):
            kwargs["database"] = ds["database"]
        params = ds.get("params") or {}
        kwargs["charset"] = params.get("charset") or "utf8mb4"
        return mysql.connector.connect(**kwargs)

    def cursor(self, conn):
        return conn.cursor(dictionary=True)

    def quote(self, ident):
        return "`" + clean_ident(ident).replace("`", "``") + "`"

    def default_schema(self, ds):
        return ds.get("database") or None

    def list_tables(self, conn, ds):
        if not ds.get("database"):
            raise DialectError(
                "该数据源未绑定具体库，SHOW TABLES 无法执行。\n"
                "请用 query 指定「库名.表名」访问，或在数据源里设置一个默认库。"
            )
        cur = conn.cursor()
        try:
            cur.execute("SHOW TABLES")
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def columns(self, conn, ds, table):
        schema, tbl = split_ref(table)
        if schema:
            sql = f"SHOW COLUMNS FROM {self.quote(schema)}.{self.quote(tbl)}"
        else:
            if not ds.get("database"):
                raise DialectError(
                    "该数据源未绑定具体库，请用「库名.表名」指定表，"
                    "或在数据源里设置一个默认库。"
                )
            sql = f"SHOW COLUMNS FROM {self.quote(tbl)}"

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql)
            rows = cur.fetchall()
        finally:
            cur.close()

        return [
            {
                "name": r.get("Field"),
                "type": r.get("Type"),
                "nullable": r.get("Null"),
                "key": r.get("Key"),
                "default": r.get("Default"),
                "extra": r.get("Extra"),
            }
            for r in rows
        ]

    def databases(self, conn):
        cur = conn.cursor()
        try:
            cur.execute("SHOW DATABASES")
            skip = {"information_schema", "mysql", "performance_schema", "sys"}
            return sorted(r[0] for r in cur.fetchall() if r[0] not in skip)
        finally:
            cur.close()

    def insert_id(self, cur):
        return getattr(cur, "lastrowid", None)


# ---------------------------------------------------------------- PostgreSQL


PG_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


class PostgresDialect(Dialect):
    name = "postgres"
    label = "PostgreSQL"
    default_port = 5432
    like_operator = "ILIKE"
    supports_cross_database = False

    def connect(self, ds):
        import psycopg
        from psycopg.rows import dict_row

        kwargs = dict(
            host=ds["host"],
            port=int(ds.get("port") or self.default_port),
            user=ds.get("user") or None,
            password=ds.get("password") or None,
            connect_timeout=10,
        )
        # PG 没有「不连库」的状态：库名为空时 psycopg 会把用户名当库名，
        # 报 "database \"<用户名>\" does not exist"。退到 postgres 维护库，
        # 连上之后再让用户「加载库列表」挑真正的库。
        kwargs["dbname"] = ds.get("database") or "postgres"
        params = ds.get("params") or {}
        if params.get("sslmode"):
            kwargs["sslmode"] = params["sslmode"]
        conn = psycopg.connect(**kwargs)
        conn.row_factory = dict_row
        return conn

    def cursor(self, conn):
        return conn.cursor()

    def quote(self, ident):
        return '"' + clean_ident(ident).replace('"', '""') + '"'

    def default_schema(self, ds):
        explicit = (ds.get("schema") or "").strip()
        if explicit:
            return explicit
        return "public"

    def list_tables(self, conn, ds):
        schema = self.default_schema(ds)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                (schema,),
            )
            return [r["table_name"] for r in cur.fetchall()]
        finally:
            cur.close()

    def columns(self, conn, ds, table):
        schema = self.default_schema(ds)
        explicit_schema, tbl = split_ref(table)
        if explicit_schema:
            schema = explicit_schema

        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT column_name, data_type, is_nullable, column_default, "
                "       character_maximum_length, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, tbl),
            )
            rows = cur.fetchall()
            if not rows:
                raise DialectError(
                    f"schema「{schema}」下没有表「{tbl}」，"
                    f"或当前账号没有查看权限。"
                )

            cur.execute(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema = %s AND tc.table_name = %s",
                (schema, tbl),
            )
            pk = {r["column_name"] for r in cur.fetchall()}
        finally:
            cur.close()

        out = []
        for r in rows:
            typename = r["data_type"]
            if r.get("character_maximum_length"):
                typename = f"{typename}({r['character_maximum_length']})"
            elif r.get("numeric_precision") and typename in ("numeric", "decimal"):
                scale = r.get("numeric_scale") or 0
                typename = f"{typename}({r['numeric_precision']},{scale})"
            out.append(
                {
                    "name": r["column_name"],
                    "type": typename,
                    "nullable": r["is_nullable"],
                    "key": "PRI" if r["column_name"] in pk else "",
                    "default": r["column_default"],
                    "extra": "",
                }
            )
        return out

    def databases(self, conn):
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT datname FROM pg_database "
                "WHERE NOT datistemplate ORDER BY datname"
            )
            return [r["datname"] for r in cur.fetchall()]
        finally:
            cur.close()

    def schemas(self, conn):
        cur = conn.cursor()
        try:
            # NOT IN %s 在 psycopg 下会被渲染成 record，必须展开成占位符列表
            marks = ", ".join(["%s"] * len(PG_SYSTEM_SCHEMAS))
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                f"WHERE schema_name NOT IN ({marks}) AND schema_name NOT LIKE 'pg_%%' "
                "ORDER BY schema_name",
                PG_SYSTEM_SCHEMAS,
            )
            return [r["schema_name"] for r in cur.fetchall()]
        finally:
            cur.close()


# ---------------------------------------------------------------------- 工厂

_REGISTRY = {
    MySQLDialect.name: MySQLDialect,
    PostgresDialect.name: PostgresDialect,
}


def get_dialect(driver):
    key = normalize_driver(driver)
    cls = _REGISTRY.get(key)
    if not cls:
        raise DialectError(
            f"不支持的驱动「{driver}」。可选：{'、'.join(sorted(_REGISTRY))}"
        )
    return cls()


def supported_drivers():
    return [
        {"value": d.name, "label": d.label, "port": d.default_port}
        for d in (MySQLDialect(), PostgresDialect())
    ]


def is_supported(driver):
    return normalize_driver(driver) in _REGISTRY


def default_port(driver, fallback=3306):
    cls = _REGISTRY.get(normalize_driver(driver))
    return cls.default_port if cls else fallback


def explain_error(exc):
    """把驱动的常见报错翻译成能自我纠正的提示。

    驱动原始报文只说了「哪里错了」，没说「该怎么改」；AI 拿到这种消息
    容易反复重试同一个错误，所以在这里补一句动作指引。
    """
    msg = str(exc)
    low = msg.lower()

    if "does not exist" in low and "database" in low:
        return (
            f"{msg}\n"
            "提示：PostgreSQL 必须连到一个已存在的库，没有「不限定库」这种状态。"
            "库名留空时驱动会拿用户名当库名，几乎必然不存在。"
            "先填 postgres（维护库）再点「加载库列表」挑真正的库。"
        )
    if "password authentication failed" in low or "access denied for user" in low:
        return f"{msg}\n提示：账号或密码不对。编辑数据源时密码留空表示沿用旧值。"
    if "could not connect" in low or "connection refused" in low or "connection timeout" in low:
        return f"{msg}\n提示：网络或实例不可达，先确认主机端口是否放通、实例是否在运行。"
    if "permission denied" in low:
        return f"{msg}\n提示：当前账号没有该对象的权限，换库或找 DBA 授权。"
    return msg
