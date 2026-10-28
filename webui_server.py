# -*- coding: utf-8 -*-
"""配置中心 Web 服务。

只监听 127.0.0.1。所有写操作走 ConfigStore.mutate，带 version 乐观锁。
密码在出站时一律脱敏，前端回传 MASK 表示「不修改」。
"""
import os
import socket
import sys
import time

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import query_log
from config_store import (
    BASE_DIR,
    MASK,
    ConfigError,
    ConfigStore,
    mask_datasource,
    new_id,
    resolve_password,
)

CONFIG_PATH = os.environ.get("DB_MCP_CONFIG") or os.path.join(BASE_DIR, "config.json")
WEBUI_DIR = os.path.join(BASE_DIR, "webui")

app = FastAPI(title="DB MCP 配置中心", version="1.0")
store = ConfigStore(CONFIG_PATH)


# ---------------------------------------------------------------- 请求模型

class DatasourceIn(BaseModel):
    alias: str = Field(min_length=1)
    env: str
    driver: str = "mysql"
    host: str = Field(min_length=1)
    port: int = 3306
    user: str = ""
    password: str | None = None
    database: str = ""          # 允许为空：不绑定具体库，查询用 db.table
    readonly: bool = False
    description: str = ""
    version: int | None = None


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)
    aliases: list[str] = []
    defaultEnv: str | None = None
    bindings: dict[str, str] = {}
    version: int | None = None


class EnvironmentIn(BaseModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    readonlyHint: bool = False
    version: int | None = None


class EnvPatch(BaseModel):
    label: str | None = None
    readonlyHint: bool | None = None
    version: int | None = None


class ReorderIn(BaseModel):
    keys: list[str]
    version: int | None = None


class GlobalIn(BaseModel):
    defaultProject: str | None = None
    defaultEnv: str | None = None
    defaultDatasource: str | None = None
    version: int | None = None


class ProbeIn(BaseModel):
    datasource_id: str | None = None
    host: str | None = None
    port: int = 3306
    user: str | None = None
    password: str | None = None
    database: str | None = None


# ---------------------------------------------------------------- 工具函数

def public_config():
    data = store.load()
    return {
        **data,
        "datasources": [mask_datasource(d) for d in data["datasources"]],
    }


def merge_password(new_val, old_val):
    """None 或 MASK 表示沿用旧值；空串表示清空。"""
    if new_val is None or new_val == MASK:
        return old_val or ""
    return new_val


def fail(e):
    msg = str(e)
    status = 409 if "已被其他窗口修改" in msg else 400
    raise HTTPException(status_code=status, detail=msg)


def find_ds(cfg, ds_id):
    for d in cfg["datasources"]:
        if d["id"] == ds_id:
            return d
    raise ConfigError(f"数据源 {ds_id} 不存在")


# ------------------------------------------------------------------ 读配置

@app.get("/api/config")
def get_config():
    return public_config()


# -------------------------------------------------------------- 执行记录

@app.get("/api/logs")
def get_logs(page: int = 1, size: int = 20):
    size = max(5, min(size, 100))
    page = max(1, page)

    items, total = query_log.page((page - 1) * size, size)
    pages = max(1, (total + size - 1) // size)

    # 页码越界时回落到最后一页，并把数据一起取回来，避免「显示 3/3 但列表为空」
    if page > pages:
        page = pages
        items, total = query_log.page((page - 1) * size, size)

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": pages,
        "stats": query_log.stats(),
    }


@app.delete("/api/logs")
def clear_logs():
    query_log.clear()
    return {"ok": True}


# ---------------------------------------------------------------- 数据源 CRUD

@app.post("/api/datasources")
def create_datasource(body: DatasourceIn):
    payload = body.model_dump()
    payload.pop("version", None)

    def fn(cfg):
        cfg["datasources"].append({
            "id": new_id("ds"),
            "params": {"charset": "utf8mb4"},
            **payload,
            "password": body.password or "",
        })

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.put("/api/datasources/{ds_id}")
def update_datasource(ds_id: str, body: DatasourceIn):
    payload = body.model_dump()
    payload.pop("version", None)

    def fn(cfg):
        ds = find_ds(cfg, ds_id)
        pwd = merge_password(body.password, ds.get("password"))
        ds.update(payload)
        ds["password"] = pwd

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.delete("/api/datasources/{ds_id}")
def delete_datasource(ds_id: str, version: int | None = None):
    def fn(cfg):
        ds = find_ds(cfg, ds_id)
        users = [
            p["name"] for p in cfg["projects"]
            if ds_id in (p.get("bindings") or {}).values()
        ]
        if users:
            raise ConfigError(
                f"数据源「{ds['alias']}」仍被 {len(users)} 个项目引用：{'、'.join(users)}，请先解绑"
            )
        if cfg["global"].get("defaultDatasource") == ds_id:
            cfg["global"]["defaultDatasource"] = None
        cfg["datasources"] = [d for d in cfg["datasources"] if d["id"] != ds_id]

    try:
        store.mutate(fn, version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.post("/api/datasources/{ds_id}/test")
def test_datasource(ds_id: str):
    ds = find_ds(store.load(), ds_id)
    return _try_connect(
        ds["host"], ds.get("port", 3306), ds.get("user", ""),
        resolve_password(ds.get("password", "")), ds.get("database") or None,
    )


def _try_connect(host, port, user, password, database=None):
    start = time.time()
    try:
        import mysql.connector

        kwargs = dict(host=host, port=int(port), user=user,
                      password=password, connection_timeout=6)
        if database:
            kwargs["database"] = database
        conn = mysql.connector.connect(**kwargs)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return {"ok": True, "ms": int((time.time() - start) * 1000)}
    except Exception as e:
        return {
            "ok": False,
            "ms": int((time.time() - start) * 1000),
            "error": f"{type(e).__name__}: {e}"[:300],
        }


@app.post("/api/probe")
def probe_databases(body: ProbeIn):
    """连库并列出可访问的数据库，避免手填库名出错。

    传 datasource_id 时用已保存的密码（编辑态前端只有掩码，拿不到明文）。
    """
    start = time.time()
    try:
        import mysql.connector

        if body.datasource_id:
            ds = find_ds(store.load(), body.datasource_id)
            host, port = ds["host"], ds.get("port", 3306)
            user = ds.get("user", "")
            pwd = resolve_password(ds.get("password", ""))
        else:
            host, port = body.host, int(body.port)
            user = body.user or ""
            pwd = resolve_password(body.password or "")

        conn = mysql.connector.connect(
            host=host, port=port, user=user, password=pwd, connection_timeout=6,
        )
        cur = conn.cursor()
        cur.execute("SHOW DATABASES")
        skip = {"information_schema", "mysql", "performance_schema", "sys"}
        dbs = sorted(r[0] for r in cur.fetchall() if r[0] not in skip)
        conn.close()
        return {"ok": True, "databases": dbs, "ms": int((time.time() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


# ------------------------------------------------------------------ 项目 CRUD

def _normalize_bindings(bindings):
    return {k: v for k, v in (bindings or {}).items() if v}


@app.post("/api/projects")
def create_project(body: ProjectIn):
    binds = _normalize_bindings(body.bindings)
    if not binds:
        raise HTTPException(400, "至少要绑定一个环境，否则这个项目查不到任何库")
    payload = body.model_dump()
    payload.pop("version", None)
    payload["bindings"] = binds
    if len(binds) == 1:
        payload["defaultEnv"] = next(iter(binds))

    def fn(cfg):
        cfg["projects"].append({"id": new_id("p"), **payload})

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.put("/api/projects/{pid}")
def update_project(pid: str, body: ProjectIn):
    binds = _normalize_bindings(body.bindings)
    if not binds:
        raise HTTPException(400, "至少要绑定一个环境，否则这个项目查不到任何库")
    payload = body.model_dump()
    payload.pop("version", None)
    payload["bindings"] = binds
    if len(binds) == 1:
        payload["defaultEnv"] = next(iter(binds))

    def fn(cfg):
        for i, p in enumerate(cfg["projects"]):
            if p["id"] == pid:
                payload["id"] = pid
                cfg["projects"][i] = payload
                return
        raise ConfigError(f"项目 {pid} 不存在")

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, version: int | None = None):
    def fn(cfg):
        before = len(cfg["projects"])
        cfg["projects"] = [p for p in cfg["projects"] if p["id"] != pid]
        if len(cfg["projects"]) == before:
            raise ConfigError(f"项目 {pid} 不存在")
        if cfg["global"].get("defaultProject") == pid:
            cfg["global"]["defaultProject"] = None

    try:
        store.mutate(fn, version)
    except ConfigError as e:
        fail(e)
    return public_config()


# ------------------------------------------------------------------ 环境 CRUD

@app.post("/api/environments")
def create_environment(body: EnvironmentIn):
    def fn(cfg):
        if any(e["key"] == body.key for e in cfg["environments"]):
            raise ConfigError(f"环境 key「{body.key}」已存在")
        cfg["environments"].append({
            "key": body.key,
            "label": body.label,
            "order": len(cfg["environments"]) + 1,
            "readonlyHint": body.readonlyHint,
        })

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.put("/api/environments/{key}")
def update_environment(key: str, body: EnvPatch):
    def fn(cfg):
        for e in cfg["environments"]:
            if e["key"] == key:
                if body.label is not None:
                    e["label"] = body.label
                if body.readonlyHint is not None:
                    e["readonlyHint"] = body.readonlyHint
                return
        raise ConfigError(f"环境「{key}」不存在")

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.delete("/api/environments/{key}")
def delete_environment(key: str, version: int | None = None):
    def fn(cfg):
        if len(cfg["environments"]) <= 1:
            raise ConfigError("至少需要保留一个环境")
        used_ds = [d["alias"] for d in cfg["datasources"] if d.get("env") == key]
        used_pj = [p["name"] for p in cfg["projects"] if (p.get("bindings") or {}).get(key)]
        if used_ds or used_pj:
            parts = []
            if used_ds:
                parts.append(f"{len(used_ds)} 个数据源（{'、'.join(used_ds[:3])}）")
            if used_pj:
                parts.append(f"{len(used_pj)} 个项目（{'、'.join(used_pj[:3])}）")
            raise ConfigError(f"无法删除环境「{key}」：仍被 {'、'.join(parts)} 引用")
        cfg["environments"] = [e for e in cfg["environments"] if e["key"] != key]
        for i, e in enumerate(cfg["environments"]):
            e["order"] = i + 1

    try:
        store.mutate(fn, version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.post("/api/environments/reorder")
def reorder_environments(body: ReorderIn):
    def fn(cfg):
        index = {e["key"]: e for e in cfg["environments"]}
        missing = [k for k in body.keys if k not in index]
        if missing:
            raise ConfigError(f"环境不存在：{'、'.join(missing)}")
        cfg["environments"] = [index[k] for k in body.keys]
        for i, e in enumerate(cfg["environments"]):
            e["order"] = i + 1

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


@app.put("/api/global")
def update_global(body: GlobalIn):
    def fn(cfg):
        if body.defaultDatasource is not None:
            if body.defaultDatasource:
                ids = {d["id"] for d in cfg["datasources"]}
                if body.defaultDatasource not in ids:
                    raise ConfigError("默认数据源不存在")
                cfg["global"]["defaultDatasource"] = body.defaultDatasource
            else:
                cfg["global"]["defaultDatasource"] = None
        if body.defaultProject is not None:
            ids = {p["id"] for p in cfg["projects"]}
            if body.defaultProject and body.defaultProject not in ids:
                raise ConfigError("默认项目不存在")
            cfg["global"]["defaultProject"] = body.defaultProject or None
        if body.defaultEnv is not None:
            keys = {e["key"] for e in cfg["environments"]}
            if body.defaultEnv and body.defaultEnv not in keys:
                raise ConfigError("默认环境不存在")
            cfg["global"]["defaultEnv"] = body.defaultEnv or None

    try:
        store.mutate(fn, body.version)
    except ConfigError as e:
        fail(e)
    return public_config()


# -------------------------------------------------------------------- 静态

if os.path.isdir(WEBUI_DIR):
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="static")


@app.middleware("http")
async def no_store(request, call_next):
    """页面和接口一律禁用缓存。

    这是本机工具，前端改动频繁；浏览器默认会对无 Cache-Control 的响应做
    启发式缓存，导致改完刷新还是旧页面。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/")
def index():
    path = os.path.join(WEBUI_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "webui/index.html 不存在"}, status_code=500)
    return FileResponse(path)


@app.exception_handler(ConfigError)
async def config_error_handler(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# -------------------------------------------------------------------- 启动

def pick_port(start=8765, tries=25):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def main():
    if not os.path.exists(CONFIG_PATH):
        print("config.json 不存在，正在从 .env 生成…")
        import migrate_env
        migrate_env.main()

    port = pick_port()
    url = f"http://127.0.0.1:{port}"

    print("=" * 54, flush=True)
    print(f"  DB MCP 配置中心  ->  {url}", flush=True)
    print(f"  配置文件          ->  {CONFIG_PATH}", flush=True)
    print("  仅本机可访问，Ctrl+C 停止", flush=True)
    print("=" * 54, flush=True)

    if "--no-browser" not in sys.argv:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
