# -*- coding: utf-8 -*-
"""配置读写层：原子写、mtime 热加载、密码解析与脱敏、数据源解析。

被 db_mcp_server.py 与 webui_server.py 共用，两边必须走同一套解析逻辑，
否则会出现「界面显示查测试库、AI 实际查了生产库」这种不可排查的问题。
"""
import json
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 便于测试或部署到其他位置：DB_MCP_CONFIG 指向别的配置文件
DEFAULT_PATH = os.environ.get("DB_MCP_CONFIG") or os.path.join(BASE_DIR, "config.json")

VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
MASK = "******"

DEFAULT_CONFIG = {
    "version": 1,
    "environments": [
        {"key": "dev", "label": "开发", "order": 1, "readonlyHint": False},
        {"key": "test", "label": "测试", "order": 2, "readonlyHint": False},
        {"key": "staging", "label": "预发", "order": 3, "readonlyHint": False},
        {"key": "prod", "label": "生产", "order": 4, "readonlyHint": True},
    ],
    "global": {"defaultProject": None, "defaultEnv": "test"},
    "datasources": [],
    "projects": [],
}


class ConfigError(Exception):
    """配置或解析错误。消息会直接回给调用方（含 AI），必须写得可自纠。"""


class ConflictError(ConfigError):
    """并发写冲突。"""


def new_id(prefix):
    return f"{prefix}_{int(time.time() * 1000):x}"


def resolve_password(raw):
    """把 ${VAR} 换成环境变量值；不是变量语法则原样返回。"""
    if not raw:
        return ""
    m = VAR_RE.match(str(raw).strip())
    if m:
        return os.getenv(m.group(1), "")
    return str(raw)


def mask_password(raw):
    """给前端/日志看的脱敏值。变量引用保留原样，便于识别来源。"""
    if not raw:
        return ""
    if VAR_RE.match(str(raw).strip()):
        return raw
    return MASK


def mask_datasource(ds):
    d = dict(ds)
    d["password"] = mask_password(ds.get("password", ""))
    return d


class ConfigStore:
    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self._data = None
        self._mtime = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 读写

    def load(self, force=False):
        """按 mtime 判断是否需要重读，实现「GUI 改完 MCP 立即生效」。"""
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                mtime = None

            if not force and self._data is not None and mtime == self._mtime:
                return self._data

            if mtime is None:
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self._mtime = None
                return self._data

            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            data = self._normalize(data)
            self._data = data
            self._mtime = mtime
            return data

    def save(self, data):
        with self._lock:
            data = self._normalize(data)
            self._validate(data)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._data = data
            self._mtime = os.path.getmtime(self.path)
            return data

    def mutate(self, fn, expected_version=None):
        """读最新配置 → 交给 fn 修改副本 → 写回。

        fn 改的是深拷贝：校验失败时不会污染内存缓存（否则一次失败会让后续读写
        看到从未落盘的脏数据）。expected_version 用于乐观锁。
        """
        with self._lock:
            data = self.load(force=True)
            if expected_version is not None and data.get("version") != expected_version:
                raise ConflictError(
                    f"配置已被其他窗口修改（当前版本 {data.get('version')}），请刷新后重试"
                )
            working = json.loads(json.dumps(data))
            fn(working)
            working["version"] = int(data.get("version", 0)) + 1
            return self.save(working)

    # ------------------------------------------------------------------ 校验

    def _normalize(self, data):
        d = {
            "version": int(data.get("version", 1)),
            "environments": data.get("environments") or [],
            "global": data.get("global") or {},
            "datasources": data.get("datasources") or [],
            "projects": data.get("projects") or [],
        }
        d["environments"].sort(key=lambda e: e.get("order", 0))
        for i, e in enumerate(d["environments"]):
            e["order"] = i + 1
            e.setdefault("readonlyHint", False)
        d["global"].setdefault("defaultProject", None)
        d["global"].setdefault("defaultEnv", None)
        d["global"].setdefault("defaultDatasource", None)
        for ds in d["datasources"]:
            ds.setdefault("driver", "mysql")
            ds.setdefault("port", 3306)
            ds.setdefault("readonly", False)
            ds.setdefault("params", {})
            ds.setdefault("description", "")
        for p in d["projects"]:
            p.setdefault("aliases", [])
            p.setdefault("bindings", {})
            p.setdefault("defaultEnv", None)
        return d

    def _validate(self, data):
        if not data["environments"]:
            raise ConfigError("至少需要保留一个环境")

        env_keys = [e["key"] for e in data["environments"]]
        if len(set(env_keys)) != len(env_keys):
            raise ConfigError("环境 key 重复")
        for k in env_keys:
            if not re.match(r"^[a-z][a-z0-9_]*$", k):
                raise ConfigError(f"环境 key「{k}」不合法，只能小写字母开头、含字母数字下划线")

        ds_ids, alias_seen = set(), {}
        for ds in data["datasources"]:
            if ds["id"] in ds_ids:
                raise ConfigError(f"数据源 id 重复：{ds['id']}")
            ds_ids.add(ds["id"])
            a = ds.get("alias", "").strip()
            if not a:
                raise ConfigError("数据源别名不能为空")
            if a in alias_seen:
                raise ConfigError(f"数据源别名「{a}」重复")
            alias_seen[a] = ds["id"]
            if ds.get("env") not in env_keys:
                raise ConfigError(f"数据源「{a}」的环境「{ds.get('env')}」不存在")
            if ds.get("driver") != "mysql":
                raise ConfigError(f"数据源「{a}」暂只支持 mysql")
            if not ds.get("host"):
                raise ConfigError(f"数据源「{a}」缺少主机地址")
            # database 允许为空：此时连接不绑定具体库，查询需用 db.table 全限定名

        name_seen = set()
        for p in data["projects"]:
            n = p.get("name", "").strip()
            if not n:
                raise ConfigError("项目名不能为空")
            if n in name_seen:
                raise ConfigError(f"项目名「{n}」重复")
            name_seen.add(n)
            binds = {k: v for k, v in (p.get("bindings") or {}).items() if v}
            if not binds:
                raise ConfigError(f"项目「{n}」至少要绑定一个环境")
            for k, v in binds.items():
                if k not in env_keys:
                    raise ConfigError(f"项目「{n}」绑定的环境「{k}」不存在")
                if v not in ds_ids:
                    raise ConfigError(f"项目「{n}」绑定的数据源「{v}」不存在")
            if p.get("defaultEnv") and p["defaultEnv"] not in binds:
                raise ConfigError(f"项目「{n}」的默认环境未绑定")

        dd = data["global"].get("defaultDatasource")
        if dd and dd not in ds_ids:
            raise ConfigError("默认数据源指向的数据源已不存在，请在配置界面重新设置")

    # ------------------------------------------------------------- 派生与查找

    @staticmethod
    def effective_default_env(project):
        """绑定 1 个环境时自动成为默认，无需配置 defaultEnv。"""
        binds = {k: v for k, v in (project.get("bindings") or {}).items() if v}
        if len(binds) == 1:
            return next(iter(binds))
        de = project.get("defaultEnv")
        if de and binds.get(de):
            return de
        return None

    @staticmethod
    def _find_datasource(data, key):
        key = str(key).strip()
        for ds in data["datasources"]:
            if ds["id"] == key:
                return ds
        low = key.lower()
        for ds in data["datasources"]:
            if ds.get("alias", "").lower() == low:
                return ds
        return None

    @staticmethod
    def _find_project(data, name):
        key = str(name).strip().lower()
        projects = data["projects"]

        for p in projects:
            if p.get("name", "").lower() == key:
                return p
        for p in projects:
            if any(a.lower() == key for a in p.get("aliases", [])):
                return p

        hits = [
            p for p in projects
            if key in p.get("name", "").lower()
            or any(key in a.lower() for a in p.get("aliases", []))
        ]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = "、".join(p["name"] for p in hits)
            raise ConfigError(f"项目「{name}」匹配到多个结果：{names}。请使用更精确的名称")
        return None

    # ---------------------------------------------------------------- 解析入口

    def resolve(self, project=None, env=None, datasource=None):
        """解析出目标数据源，返回 (datasource, 实际 env, 解析方式)。

        显式指定 env 必须精确命中，绝不降级——降级会让人以为在查生产、实际查了测试，
        这种静默给错数据比直接报错危险。解析方式用于日志，便于事后判断是 AI 指定的
        还是走的默认（见 README「目标解析优先级」）。
        """
        data = self.load()

        if datasource:
            ds = self._find_datasource(data, datasource)
            if not ds:
                avail = "、".join(d["alias"] for d in data["datasources"]) or "（无）"
                raise ConfigError(f"数据源「{datasource}」不存在。可用：{avail}")
            return ds, ds.get("env"), "datasource 指定"

        env_keys = [e["key"] for e in data["environments"]]

        if project:
            p = self._find_project(data, project)
            if not p:
                avail = "、".join(x["name"] for x in data["projects"]) or "（无）"
                raise ConfigError(f"项目「{project}」不存在。可用：{avail}")
            ds, used_env = self._resolve_project(data, p, env, env_keys)
            return ds, used_env, f"项目「{p['name']}」"

        # 未指定项目：优先默认数据源，其次全局默认项目
        dd = data["global"].get("defaultDatasource")
        if dd:
            ds = self._find_datasource(data, dd)
            if not ds:
                raise ConfigError("默认数据源已失效，请在配置界面重新设置")
            return ds, ds.get("env"), "默认数据源"

        gp = data["global"].get("defaultProject")
        if gp:
            p = self._find_project(data, gp)
            if not p:
                raise ConfigError(f"全局默认项目「{gp}」已失效，请在配置界面修正")
            ds, used_env = self._resolve_project(data, p, env, env_keys)
            return ds, used_env, "默认项目"

        raise ConfigError(
            "未指定数据源。请传入 project / env / datasource 之一，"
            "或先在配置界面设置一个默认数据源"
        )

    def _resolve_project(self, data, p, env, env_keys):
        binds = {k: v for k, v in (p.get("bindings") or {}).items() if v}
        bound_desc = "、".join(f"{k}→{v}" for k, v in binds.items()) or "（无）"

        if env:
            if env not in env_keys:
                raise ConfigError(
                    f"环境「{env}」不存在。可用环境：{'、'.join(env_keys)}"
                )
            if env not in binds:
                raise ConfigError(
                    f"项目「{p['name']}」未绑定环境「{env}」。已绑定：{bound_desc}"
                )
            ds = self._find_datasource(data, binds[env])
            if not ds:
                raise ConfigError(f"项目「{p['name']}」绑定的数据源「{binds[env]}」已不存在")
            return ds, env

        de = self.effective_default_env(p)
        if not de:
            raise ConfigError(
                f"项目「{p['name']}」绑定了多个环境（{bound_desc}），"
                f"但未设置默认环境。请指定 env，或在配置界面设置默认环境"
            )
        ds = self._find_datasource(data, binds[de])
        if not ds:
            raise ConfigError(f"项目「{p['name']}」绑定的数据源「{binds[de]}」已不存在")
        return ds, de

    # ------------------------------------------------------------------ 展示

    def list_projects(self):
        data = self.load()
        out = []
        for p in data["projects"]:
            binds = {}
            for k, v in (p.get("bindings") or {}).items():
                if not v:
                    continue
                ds = self._find_datasource(data, v)
                if ds:
                    binds[k] = ds["alias"]
            out.append({
                "name": p["name"],
                "aliases": p.get("aliases", []),
                "defaultEnv": self.effective_default_env(p),
                "bindings": binds,
            })
        return out

    def list_datasources(self):
        data = self.load()
        return [
            {
                "alias": ds["alias"],
                "env": ds.get("env"),
                "driver": ds.get("driver"),
                "host": ds.get("host"),
                "port": ds.get("port"),
                "database": ds.get("database"),
                "readonly": bool(ds.get("readonly")),
            }
            for ds in data["datasources"]
        ]


_default_store = None


def get_store(path=None):
    global _default_store
    if _default_store is None or (path and path != _default_store.path):
        _default_store = ConfigStore(path)
    return _default_store
