#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weverse 补货监控：检测商品从售罄变为有货，并通过飞书开放接口推送私聊提醒。

无需第三方依赖，仅使用 Python 标准库。
通知方式：使用自建应用（app_id/app_secret）获取 tenant_access_token，
以 Bot 身份给指定用户发送私聊消息（应用需已开通 im:message:send_as_bot）。
抓取策略：先直接请求商品页；失败时退回 r.jina.ai 渲染代理，保证 SPA 页面也能拿到渲染后的内容。
状态去重：通过 state.json（GitHub Actions cache 持久化）记录上次状态，仅在「售罄 -> 有货」变化时通知。
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request

# ---------------- 监控商品配置 ----------------
PRODUCTS = [
    {
        "id": "43782",
        "artist": "TOMORROW X TOGETHER",
        "name": "PLUSH KEYRING",
        "price": "₩20,000",
        "url": "https://shop.weverse.io/zh-cn/shop/KRW/artists/3/sales/43782",
    },
    {
        "id": "52098",
        "artist": "BOYNEXTDOOR",
        "name": "Plush Keyring Set",
        "price": "₩135,455",
        "url": "https://shop.weverse.io/zh-cn/shop/KRW/artists/112/sales/52098",
    },
]

# ---------------- 通知目标 ----------------
# 补货提醒将发送到该用户的飞书私聊
NOTIFY_OPEN_ID = "ou_1d6ad702e2905d580b25fea809d2992f"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
SOLD_OUT_MARKERS = ["售罄", "SOLD OUT", "品切れ", "품절", "Agotado"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def http_get(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_page(url):
    """直接抓取失败时，退回 r.jina.ai 渲染代理。"""
    for attempt in (1, 2):
        try:
            target = url if attempt == 1 else "https://r.jina.ai/" + url
            text = http_get(target, timeout=60 if attempt == 2 else 30)
            if len(text) > 200:
                return text
        except Exception as exc:
            print(f"fetch attempt {attempt} failed for {url}: {exc}")
        time.sleep(3)
    return ""


def detect(text, name):
    """根据页面文本判断商品状态。unknown 表示抓取异常，不能下结论。"""
    if not text or name.lower() not in text.lower():
        return "unknown"
    lowered = text.lower()
    sold_out = any(marker.lower() in lowered for marker in SOLD_OUT_MARKERS)
    return "sold_out" if sold_out else "in_stock"


def get_tenant_token(app_id, app_secret):
    """用应用凭证获取 tenant_access_token。"""
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("tenant_access_token")


def feishu_send(text):
    """以 Bot 身份给指定用户发送私聊文本消息。"""
    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    if not (app_id and app_secret):
        print("LARK_APP_ID / LARK_APP_SECRET 未配置，跳过发送。通知内容：\n" + text)
        return False

    token = get_tenant_token(app_id, app_secret)
    if not token:
        print("获取 tenant_access_token 失败，请检查应用凭证")
        return False

    payload = {
        "receive_id": NOTIFY_OPEN_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    print("feishu response:", body)
    if body.get("code") != 0:
        print("飞书消息发送失败，请检查应用权限与用户可用范围")
        return False
    return True


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def main():
    state = load_state()
    first_run = not state
    for product in PRODUCTS:
        text = fetch_page(product["url"])
        status = detect(text, product["name"])
        prev = state.get(product["id"])
        print(f"{product['name']}: prev={prev or 'baseline'} now={status}")
        if status == "unknown":
            continue  # 抓取异常时不下结论，也不更新状态
        state[product["id"]] = status
        if prev == "sold_out" and status == "in_stock":
            message = (
                f"【补货提醒】{product['artist']} - {product['name']} 已补货！\n"
                f"价格：{product['price']}\n"
                f"购买链接：{product['url']}\n"
                f"请尽快下单！"
            )
            feishu_send(message)
    save_state(state)
    if first_run:
        print("首次运行：已记录基线状态，不发送通知。")


if __name__ == "__main__":
    main()
