# -*- coding: utf-8 -*-
"""把现有 .env 迁移成 config.json 的一个数据源。

幂等：config.json 已存在时直接退出，不覆盖。
.db 里 DB_NAME 为空时跳过数据源迁移（数据库名不能猜），只生成环境骨架。
"""
import json
import os
import sys

from config_store import BASE_DIR, DEFAULT_CONFIG, ConfigStore, new_id

ENV_PATH = os.path.join(BASE_DIR, ".env")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def read_env(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main():
    if os.path.exists(CONFIG_PATH):
        print(f"config.json 已存在，跳过迁移：{CONFIG_PATH}")
        return 0

    env = read_env(ENV_PATH)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    host = env.get("DB_HOST", "").strip()
    port = env.get("DB_PORT", "3306").strip() or "3306"
    user = env.get("DB_USER", "").strip()
    password = env.get("DB_PASSWORD", "")
    database = env.get("DB_NAME", "").strip()

    if not host:
        print("未发现 .env 或缺少 DB_HOST，仅生成空配置骨架")
    elif not database:
        print("检测到 DB_NAME 为空，已跳过数据源迁移（数据库名不能猜测）")
        print(f"  .env 中的连接信息：{user}@{host}:{port}")
        print("  打开配置界面后点「新增数据源」补全即可")
    else:
        ds = {
            "id": new_id("ds"),
            "alias": f"default-{database}",
            "env": "test",
            "driver": "mysql",
            "host": host,
            "port": int(port) if port.isdigit() else 3306,
            "user": user,
            "password": password,
            "database": database,
            "params": {"charset": "utf8mb4"},
            "readonly": False,
            "description": "从 .env 迁移",
        }
        cfg["datasources"].append(ds)
        print(f"已迁移数据源：{ds['alias']} ({host}:{port}/{database})")

    store = ConfigStore(CONFIG_PATH)
    store.save(cfg)
    print(f"已生成 {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
