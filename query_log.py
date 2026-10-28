# -*- coding: utf-8 -*-
"""SQL 执行日志：追加写入 JSONL，供配置界面的「执行记录」面板读取。

设计取舍：
- 用 JSONL 而不是 SQLite —— 只追加、不更新，无需事务，且可 tail 读取不必载入全量
- 写入失败一律吞掉，日志不能影响查询主流程
- 超过 5MB 轮转为 .1，只保留一代，避免无限增长
"""
import json
import os
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.environ.get("DB_MCP_LOG") or os.path.join(BASE_DIR, "query_log.jsonl")
MAX_BYTES = 5 * 1024 * 1024

_lock = threading.Lock()


def record(tool, ds, env, sql, ok=True, rows=None, ms=None, error=None,
           project=None, params=None, resolved_by=None):
    """写一条执行记录。任何异常都不向外抛。

    resolved_by 记录「这个数据源是怎么选出来的」（datasource 指定 / 项目 / 默认数据源），
    排查「为什么查到了另一个库」时是唯一的线索。
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "project": project,
        "env": env,
        "datasource": (ds or {}).get("alias"),
        "database": (ds or {}).get("database"),
        "sql": sql,
        "params": params if params else None,
        "rows": rows,
        "ms": ms,
        "ok": bool(ok),
        "error": error,
        "resolvedBy": resolved_by,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        try:
            _rotate()
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _rotate():
    try:
        if os.path.getsize(LOG_PATH) > MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def tail(limit=60):
    """返回最近 limit 条，最新在前。"""
    items, _ = page(0, limit)
    return items


def page(offset=0, limit=20):
    """按「从最新往前」分页，返回 (items, total)。

    offset=0 是最近一页，offset=20 是再往前 20 条。
    日志是新写追加到文件末尾，所以从尾部切片最自然。
    """
    if not os.path.exists(LOG_PATH):
        return [], 0

    with _lock:
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return [], 0

    total = len(lines)
    end = max(0, total - offset)
    start = max(0, end - limit)

    out = []
    for line in lines[start:end]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()
    return out, total


def stats(limit=500):
    """基于最近 limit 条计算概览。"""
    items = tail(limit)
    if not items:
        return {"total": 0, "ok": 0, "fail": 0, "avgMs": 0, "topProjects": []}

    ok = sum(1 for x in items if x.get("ok"))
    ms = [x["ms"] for x in items if isinstance(x.get("ms"), int)]
    counts = {}
    for x in items:
        name = x.get("project") or x.get("datasource") or "未指定"
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]

    return {
        "total": len(items),
        "ok": ok,
        "fail": len(items) - ok,
        "avgMs": int(sum(ms) / len(ms)) if ms else 0,
        "topProjects": [{"name": k, "count": v} for k, v in top],
    }


def clear():
    with _lock:
        try:
            os.remove(LOG_PATH)
        except OSError:
            pass
        try:
            os.remove(LOG_PATH + ".1")
        except OSError:
            pass
