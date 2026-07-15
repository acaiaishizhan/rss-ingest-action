# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from feishu_client import get_tenant_access_token, list_bitable_fields


def delete_bitable_field(app_token: str, table_id: str, tenant_token: str, field_id: str) -> bool:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    resp = requests.delete(url, headers=headers, timeout=config.HTTP_TIMEOUT)
    try:
        data = resp.json()
    except Exception:
        return False
    return data.get("code") == 0


def main() -> None:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )

    items = list_bitable_fields(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_RSS_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )

    target_names = {
        "priority",
        "fetch_interval",
        "content_hash_algo",
        "avg_items_per_day",
        "quality_score",
        "owner",
        "created_at",
        "updated_at",
    }

    deleted = 0
    missing = 0
    for item in items:
        name = item.get("field_name") or item.get("name")
        if name not in target_names:
            continue
        field_id = item.get("field_id") or item.get("id")
        if not field_id:
            missing += 1
            continue
        ok = delete_bitable_field(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_RSS_TABLE_ID,
            tenant_token,
            field_id,
        )
        if ok:
            deleted += 1
        else:
            print(f"[Feishu] delete field failed: {name} id={field_id}")

    print(f"[Delete] deleted={deleted}, missing_id={missing}")


if __name__ == "__main__":
    main()
