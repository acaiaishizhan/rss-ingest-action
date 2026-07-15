import argparse
from datetime import datetime, timedelta, timezone
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from feishu_client import get_tenant_access_token, http_post
from rss_ingest import clean_feishu_value

config: Any = None


def parse_ts_ms(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    if isinstance(value, dict) and "value" in value:
        return parse_ts_ms(value.get("value"))
    return None


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            m = re.search(r"(\d+(\.\d+)?)", s)
            if not m:
                return None
            num = float(m.group(1))
            if "分钟" in s:
                return num / 60.0
            if "天" in s:
                return num * 24.0
            return num
    if isinstance(value, dict) and "value" in value:
        return parse_float(value.get("value"))
    if isinstance(value, list):
        if not value:
            return None
        return parse_float(value[0])
    return None


def load_env_file(path):
    if not path or not os.path.isfile(path):
        return []
    loaded = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key:
                os.environ[key] = value
                loaded.append(key)
    return loaded


def iter_recent_records(tenant_token, cutoff_ms, sort_field, page_size=200, max_pages=50):
    if config is None:
        raise RuntimeError("config 未加载")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.FEISHU_APP_TOKEN}/tables/{config.FEISHU_NEWS_TABLE_ID}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    sort = [{"field_name": sort_field, "desc": True}] if sort_field else None
    page_token = None

    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": page_size}
        body: dict[str, Any] = {}
        if sort:
            body["sort"] = sort
        if page_token:
            params["page_token"] = page_token
        resp = http_post(url, headers, body, config.HTTP_TIMEOUT, config.HTTP_RETRIES, params=params)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list records error: {data}")
        data_block = data.get("data") or {}
        items = data_block.get("items") or []
        if not items:
            break

        for record in items:
            fields = record.get("fields") or {}
            ts = parse_ts_ms(fields.get(config.NEWS_FIELD_PUBLISHED_MS))
            if not ts:
                ts = parse_ts_ms(fields.get(config.NEWS_FIELD_CREATED_TIME))
            if ts and ts < cutoff_ms:
                return
            yield record

        if not data_block.get("has_more"):
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break


def format_records(records, hours, now):
    cutoff = now - timedelta(hours=hours)
    lines = []
    lines.append(f"飞书新闻（最近 {hours} 小时）")
    lines.append(f"时间范围：{cutoff.strftime('%Y-%m-%d %H:%M:%S')} ~ {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    count = 0
    for record in records:
        fields = record.get("fields") or {}
        title = clean_feishu_value(fields.get(config.NEWS_FIELD_TITLE)).strip() or "（无标题）"
        summary = clean_feishu_value(fields.get(config.NEWS_FIELD_SUMMARY)).strip()
        ts = parse_ts_ms(fields.get(config.NEWS_FIELD_PUBLISHED_MS)) or parse_ts_ms(fields.get(config.NEWS_FIELD_CREATED_TIME))
        if ts:
            published = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
            published_str = published.strftime("%Y-%m-%d %H:%M:%S")
        else:
            published_str = "未知"

        lines.append(f"{count + 1}. {title}")
        lines.append(f"   发布时间：{published_str}")
        if summary:
            lines.append(f"   总结：{summary}")
        else:
            lines.append("   总结：（空）")
        lines.append("")
        count += 1

    if count == 0:
        lines.append(f"（近 {hours:g} 小时内无记录）")

    return "\n".join(lines), count


def fetch_top_records(tenant_token, sort_field, limit=10, page_size=200, max_pages=5):
    if config is None:
        raise RuntimeError("config 未加载")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.FEISHU_APP_TOKEN}/tables/{config.FEISHU_NEWS_TABLE_ID}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    sort = [{"field_name": sort_field, "desc": True}] if sort_field else None
    page_token = None
    collected = []

    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": page_size}
        body: dict[str, Any] = {}
        if sort:
            body["sort"] = sort
        if page_token:
            params["page_token"] = page_token
        resp = http_post(url, headers, body, config.HTTP_TIMEOUT, config.HTTP_RETRIES, params=params)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list records error: {data}")
        data_block = data.get("data") or {}
        items = data_block.get("items") or []
        for record in items:
            collected.append(record)
            if len(collected) >= limit:
                return collected
        if not data_block.get("has_more"):
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break
    return collected


def scan_all_records(tenant_token, hours, page_size=200, max_pages=200):
    if config is None:
        raise RuntimeError("config 未加载")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.FEISHU_APP_TOKEN}/tables/{config.FEISHU_NEWS_TABLE_ID}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    page_token = None
    now = datetime.now().astimezone()
    cutoff_ms = int((now - timedelta(hours=hours)).timestamp() * 1000)
    total = 0
    within_published = 0
    within_created = 0
    max_published = None
    max_created = None

    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": page_size}
        body: dict[str, Any] = {}
        if page_token:
            params["page_token"] = page_token
        resp = http_post(url, headers, body, config.HTTP_TIMEOUT, config.HTTP_RETRIES, params=params)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list records error: {data}")
        data_block = data.get("data") or {}
        items = data_block.get("items") or []
        if not items:
            break

        for record in items:
            total += 1
            fields = record.get("fields") or {}
            ts_pub = parse_ts_ms(fields.get(config.NEWS_FIELD_PUBLISHED_MS))
            ts_created = parse_ts_ms(fields.get(config.NEWS_FIELD_CREATED_TIME))
            if ts_pub:
                if max_published is None or ts_pub > max_published:
                    max_published = ts_pub
                if ts_pub >= cutoff_ms:
                    within_published += 1
            if ts_created:
                if max_created is None or ts_created > max_created:
                    max_created = ts_created
                if ts_created >= cutoff_ms:
                    within_created += 1

        if not data_block.get("has_more"):
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break

    return {
        "total": total,
        "max_published": max_published,
        "max_created": max_created,
        "within_published": within_published,
        "within_created": within_created,
        "cutoff_ms": cutoff_ms,
        "now": now,
    }


def iter_distance_records(tenant_token, max_hours, sort_field="距今", page_size=200, max_pages=50):
    if config is None:
        raise RuntimeError("config 未加载")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.FEISHU_APP_TOKEN}/tables/{config.FEISHU_NEWS_TABLE_ID}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    sort = [{"field_name": sort_field, "desc": False}] if sort_field else None
    page_token = None

    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": page_size}
        body: dict[str, Any] = {}
        if sort:
            body["sort"] = sort
        if page_token:
            params["page_token"] = page_token
        resp = http_post(url, headers, body, config.HTTP_TIMEOUT, config.HTTP_RETRIES, params=params)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list records error: {data}")
        data_block = data.get("data") or {}
        items = data_block.get("items") or []
        if not items:
            break

        for record in items:
            fields = record.get("fields") or {}
            distance = parse_float(fields.get("距今"))
            if distance is None:
                continue
            if distance <= max_hours:
                yield record
            elif sort_field == "距今":
                # 已按“距今”升序，超过阈值可提前结束
                return

        if not data_block.get("has_more"):
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break


def main():
    parser = argparse.ArgumentParser(description="Export recent Feishu news to a txt file.")
    parser.add_argument("--hours", type=float, default=12.0, help="Look back hours, default 12.")
    parser.add_argument("--output", default="out/feishu_news_last_12h.txt", help="Output txt file path.")
    parser.add_argument("--env", default=None, help="Path to .env file for config.")
    parser.add_argument("--debug", action="store_true", help="Print recent records and field diagnostics.")
    parser.add_argument("--debug-limit", type=int, default=10, help="Debug: how many records to print.")
    parser.add_argument(
        "--sort-field",
        default="published",
        choices=["published", "created"],
        help="Sort by published or created time.",
    )
    parser.add_argument("--scan-all", action="store_true", help="Scan all records to find latest timestamps.")
    parser.add_argument("--use-distance", action="store_true", help="Filter by field 距今 <= hours.")
    parser.add_argument("--max-pages", type=int, default=50, help="Max pages to scan.")
    args = parser.parse_args()

    env_path = args.env
    if not env_path:
        if os.path.isfile("F:\\coding\\rss-ingest-local\\rss-ingest-local.env"):
            env_path = "F:\\coding\\rss-ingest-local\\rss-ingest-local.env"
        elif os.path.isfile(".env"):
            env_path = ".env"
    loaded = load_env_file(env_path)
    if loaded:
        print(f"已加载环境变量：{', '.join(loaded)}")

    global config
    config = importlib.import_module("config")

    missing = []
    if not config.FEISHU_APP_ID:
        missing.append("FEISHU_APP_ID")
    if not config.FEISHU_APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if not config.FEISHU_APP_TOKEN:
        missing.append("FEISHU_APP_TOKEN")
    if not config.FEISHU_NEWS_TABLE_ID:
        missing.append("FEISHU_NEWS_TABLE_ID")
    if missing:
        print(f"缺少环境变量：{', '.join(missing)}", file=sys.stderr)
        return 2

    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)

    now = datetime.now().astimezone()
    cutoff_ms = int((now - timedelta(hours=args.hours)).timestamp() * 1000)
    sort_field = config.NEWS_FIELD_PUBLISHED_MS if args.sort_field == "published" else config.NEWS_FIELD_CREATED_TIME
    if args.use_distance:
        records = iter_distance_records(tenant_token, args.hours, sort_field="距今", max_pages=args.max_pages)
    else:
        records = iter_recent_records(tenant_token, cutoff_ms, sort_field, max_pages=args.max_pages)
    content, count = format_records(records, args.hours, now)

    with open(args.output, "w", encoding="utf-8-sig") as f:
        f.write(content)

    print(f"已写入 {args.output}，共 {count} 条记录。")
    if args.debug:
        print("---- 调试信息 ----")
        print(f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"截止时间(ms)：{cutoff_ms}")
        for label, field in (
            ("按发布时间排序", config.NEWS_FIELD_PUBLISHED_MS),
            ("按创建时间排序", config.NEWS_FIELD_CREATED_TIME),
        ):
            recent = fetch_top_records(tenant_token, sort_field=field, limit=args.debug_limit)
            print(f"-- {label} --")
            if not recent:
                print("未获取到任何记录（可能表为空或权限不足）。")
                continue
            for idx, record in enumerate(recent, 1):
                fields = record.get("fields") or {}
                if idx == 1:
                    print(f"字段键示例：{', '.join(sorted(fields.keys()))}")
                raw_pub = fields.get(config.NEWS_FIELD_PUBLISHED_MS)
                raw_created = fields.get(config.NEWS_FIELD_CREATED_TIME)
                raw_distance = fields.get("距今")
                ts_pub = parse_ts_ms(raw_pub)
                ts_created = parse_ts_ms(raw_created)
                title = clean_feishu_value(fields.get(config.NEWS_FIELD_TITLE)).strip()
                print(f"{idx}. 标题：{title}")
                print(f"   raw 发布时间：{raw_pub}")
                print(f"   raw 创建时间：{raw_created}")
                print(f"   raw 距今：{raw_distance}")
                print(f"   解析 发布时间(ms)：{ts_pub}")
                print(f"   解析 创建时间(ms)：{ts_created}")
    if args.scan_all:
        stats = scan_all_records(tenant_token, args.hours)
        print("---- 全量扫描 ----")
        print(f"记录总数：{stats['total']}")
        print(f"12 小时内（发布时间）：{stats['within_published']}")
        print(f"12 小时内（创建时间）：{stats['within_created']}")
        if stats["max_published"]:
            latest_pub = datetime.fromtimestamp(stats["max_published"] / 1000, tz=timezone.utc).astimezone()
            print(f"最新发布时间：{latest_pub.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("最新发布时间：无")
        if stats["max_created"]:
            latest_created = datetime.fromtimestamp(stats["max_created"] / 1000, tz=timezone.utc).astimezone()
            print(f"最新创建时间：{latest_created.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("最新创建时间：无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
