#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 兼容 GBK 终端：强制 stdout/stderr 使用 UTF-8（不影响排版与格式）
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")

# ==========================================================
# 功能说明：zippo会员 code 版（对齐铛铛一下.py 的 code 接口）
# 机制：code 接口获取微信 code → /api/users/auth 换 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token 并调用 /api/users/profile 验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效则重新获取 code 自动刷新。
# ==========================================================

# zippo会员 签到 / 互动任务 code 版
#
# 功能：
#   1. code 接口获取微信 code（对齐铛铛一下.py）
#   2. /api/users/auth 用 code 换 token（JWT）
#   3. 每日签到（/api/daily-signin）
#   4. 互动任务：浏览上新 + 收藏商品（上报 + 领奖）
#   5. 查询积分（/api/users/points）
#   6. 品赞代理，业务请求优先代理，失败直连兜底
#   7. PushPlus + 企业微信机器人 推送
#
# 契约（appid wxaa75ffd8c2d75da7，host wx-center.zippo.com.cn）：
#   这家没有业务成功码：成功就是 HTTP 2xx（登录/签到都回 201）且响应体里没有 code；
#   失败才带 code，且放在 4xx 的 JSON 体里 —— 重复签到 = HTTP 400
#     {"code":"already_signed","message":"今日已签到"}，所以不能在非 200 时直接抛。
#
# 环境变量：
#   PLUSPLUS_TOKEN    PushPlus token，可选
#   QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
#   PROXY_API         品赞代理提取 API，可选
#   PROXY_TYPE        http / socks5，默认 http
#   ZIPPO_TOKEN       兜底 Token（可选，仅当 code 服务失效时使用）
#   ZIPPO_DEBUG       置 1 打印接口原始响应，便于核对字段名
#   CODE_SERVER       覆盖 code 服务地址，可选
#
# 依赖：
#   pip install requests
#   socks5 代理需：pip install requests[socks]

import json
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "zippo会员"
APPID = "wxaa75ffd8c2d75da7"
BASE = "https://wx-center.zippo.com.cn"

EP_LOGIN = "/api/users/auth"
EP_SIGN = "/api/daily-signin"
EP_USER = "/api/users/profile"
EP_POINTS = "/api/users/points"
EP_FAVORITES = "/api/favorites"
EP_GOODS_SEARCH = "/api/goods/search"
EP_MISSIONS = "/api/missions"
EP_MISSION_RECORDS = "/api/missions/records"

# code 接口服务（对齐铛铛一下.py 的 SERVERS）
SERVERS = [
    "10.30.9.183:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
FALLBACK_TOKEN = os.getenv("ZIPPO_TOKEN", "")
DEBUG_MODE = os.getenv("ZIPPO_DEBUG", "") == "1"
ZIPPO_BROWSE_NEW_ID = os.getenv("ZIPPO_BROWSE_NEW_ID", "").strip()
ZIPPO_FAVORITE_ID = os.getenv("ZIPPO_FAVORITE_ID", "").strip()

MISSION_CODE_BROWSE_NEW = "pageview"
MISSION_DEFAULT_ID_BROWSE_NEW = 3
MISSION_CODE_FAVORITE = "goodsfav"
MISSION_DEFAULT_ID_FAVORITE = 5

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zippo_cookies.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
)

def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🔥 zippo会员 code 版                         ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 🌍 来源 {server:<40}│")
    print("└" + "─" * 50 + "┘")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None

        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass

    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }

    return None


def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    print(f"🌐 [代理] {account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue

            print(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            print(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)



def send_qywx(title, content):
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时自动跳过。"""
    if not QYWX_TOKEN:
        print("[企业微信] 未配置 QYWX_TOKEN，跳过推送")
        return False
    key = QYWX_TOKEN.split("key=")[-1].strip()
    import json as _qywx_json, urllib.request as _qywx_urllib
    try:
        text = "%s\n%s" % (title, content)
        if len(text.encode("utf-8")) > 2000:
            text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
        payload = _qywx_json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = _qywx_urllib.Request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
                                   data=payload, headers={"Content-Type": "application/json"})
        res = _qywx_json.loads(_qywx_urllib.urlopen(req, timeout=10).read().decode("utf-8"))
        ok = res.get("errcode") == 0
        print("[企业微信] 推送%s errcode=%s errmsg=%s" % ("成功 ✓" if ok else "失败 ✗", res.get("errcode"), res.get("errmsg", "")))
        return ok
    except Exception as _exc:
        print("[企业微信] 推送异常:", _exc)
        return False
def send_pushplus(title: str, content: str) -> None:
    send_qywx(title, content)  # 企业微信推送（QYWX_TOKEN 未配置时自动跳过）
    if not PLUSPLUS_TOKEN:
        print("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PLUSPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=10,
        )
        print("✅ [PushPlus] 推送成功")
    except Exception as exc:
        print(f"❌ [PushPlus] 推送失败: {exc}")


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=20,
        )
        data = response.json()

        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APPID}/0/page-frame.html",
        "Accept": "application/json, text/plain, */*",
        "xweb_xhr": "1",
        "x-app-id": "zippo",
        "x-platform": "wxmp",
        "x-platform-id": APPID,
        "x-platform-env": "release",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def load_token_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        print(f"⚠️ [缓存] 读取失败: {exc}")
    return {}


def save_token_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print("✅ [缓存] Token保存成功")
    except Exception as exc:
        print(f"❌ [缓存] 保存失败: {exc}")


def get_cached_token(server: str) -> str | None:
    cache = load_token_cache()
    data = cache.get(server)
    if isinstance(data, dict) and data.get("token"):
        return data["token"]
    return None


def set_cached_token(server: str, token: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def is_ok(data: Any) -> bool:
    """zippo 契约：成功=HTTP 2xx 且响应体无 code；失败才带 code"""
    return isinstance(data, dict) and "code" not in data


def err_msg(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("msg") or data.get("code") or json_preview(data, 200))
    return str(data)


def api_request(server: str, api_path: str, token: str | None, proxies: Dict[str, str] | None,
                method: str = "POST", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """zippo 通用请求：HTTP 4xx 也返回 JSON 体（重复签到等），不抛异常"""
    headers = common_headers(token)
    kwargs: Dict[str, Any] = {"headers": headers, "proxies": proxies, "server": server}
    if method.upper() == "GET":
        response = request_with_proxy("GET", f"{BASE}{api_path}", **kwargs)
    else:
        response = request_with_proxy("POST", f"{BASE}{api_path}", json=payload or {}, **kwargs)
    try:
        data = response.json()
    except Exception:
        data = {"code": f"http_{response.status_code}", "message": response.text[:300]}
    if DEBUG_MODE:
        print(f"  🐞 [{api_path}] HTTP {response.status_code} -> {json_preview(data, 300)}")
    return data

def validate_token(server: str, token: str, proxies: Dict[str, str] | None) -> bool:
    """缓存/兜底 token 真验：调 profile 接口，无 code 即有效"""
    try:
        data = api_request(server, EP_USER, token, proxies, "GET")
        return is_ok(data)
    except Exception:
        return False


def login_by_code(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    code = get_code(server)
    if not code:
        return None, None

    print("🔐 [登录] 使用 code 换 token（/api/users/auth）")
    try:
        data = api_request(server, EP_LOGIN, None, proxies, "POST",
                           {"code": code, "scene": "1001", "platform": "wxmp"})
        if not is_ok(data):
            print(f"❌ [登录] 登录失败: {err_msg(data)}")
            return None, data

        token = str(data.get("token") or "")
        if not token:
            print(f"❌ [登录] 未返回 token: {json_preview(data, 300)}")
            return None, data

        print(f"✅ [登录] token 获取成功: {mask(token)}")
        return token, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（profile 验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        if validate_token(server, cache_token, proxies):
            print("✅ [缓存] token 有效")
            return cache_token, None
        print("⚠️ [缓存] token 已失效，重新登录")

    if FALLBACK_TOKEN:
        print("🔍 [兜底] 验证 ZIPPO_TOKEN")
        if validate_token(server, FALLBACK_TOKEN, proxies):
            print("✅ [兜底] ZIPPO_TOKEN 有效")
            set_cached_token(server, FALLBACK_TOKEN)
            return FALLBACK_TOKEN, None
        print("⚠️ [兜底] ZIPPO_TOKEN 无效，继续 code 登录")

    token, raw_login = login_by_code(server, proxies)
    if not token:
        return None, raw_login

    set_cached_token(server, token)
    return token, raw_login


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def fetch_missions(server: str, token: str, proxies: Dict[str, str] | None) -> List[Dict[str, Any]]:
    """拉取任务列表并拍平分组结构：[{type,missions:[...]}] -> 扁平任务数组"""
    flat: List[Dict[str, Any]] = []
    try:
        data = api_request(server, EP_MISSIONS, token, proxies, "GET")
        raw = data
        if isinstance(data, dict):
            for key in ("list", "data", "records", "items"):
                if isinstance(data.get(key), (list, dict)):
                    raw = data[key]
                    break
        arr = raw if isinstance(raw, list) else ([raw] if raw else [])
        for group in arr:
            if isinstance(group, dict) and isinstance(group.get("missions"), list):
                for mission in group["missions"]:
                    if isinstance(mission, dict):
                        flat.append(mission)
            elif isinstance(group, dict):
                flat.append(group)
    except Exception as exc:
        print(f"⚠️ [任务] 获取任务列表失败: {exc}")
    if DEBUG_MODE:
        print(f"  🐞 [任务] 原始任务数据: {json_preview(flat, 400)}")
    return flat


def resolve_mission_id(server: str, token: str, proxies: Dict[str, str] | None,
                       code: str, label: str, env_val: str, hard_default: int) -> int:
    """任务 id 解析：环境变量 > 接口动态解析 > 硬编码兜底（照 zippo.js 契约）"""
    env_id = _to_int(env_val)
    if env_id:
        print(f"🎯 [任务] 【{label}】使用环境变量指定 id={env_id}")
        return env_id

    missions = fetch_missions(server, token, proxies)
    code_keys = ["code", "taskCode", "missionCode", "type", "missionType", "taskType"]
    name_keys = ["name", "taskName", "missionName", "title", "label", "missionTitle"]
    id_keys = ["id", "missionId", "taskId", "mid"]

    found = None
    for key in code_keys:
        for mission in missions:
            if mission.get(key) is not None and str(mission[key]) == code:
                found = mission
                break
        if found:
            break
    if not found:
        for key in name_keys:
            for mission in missions:
                value = mission.get(key)
                if value and (label in str(value) or str(value) in label):
                    found = mission
                    break
            if found:
                break
    if found:
        for key in id_keys:
            mid = _to_int(found.get(key))
            if mid:
                print(f"🎯 [任务] 【{label}】动态解析 id={mid} (code={code})")
                return mid

    print(f"⚠️ [任务] 【{label}】无法解析任务ID，兜底 id={hard_default}"
          f"（后台改过任务ID会失效，可设 ZIPPO_BROWSE_NEW_ID / ZIPPO_FAVORITE_ID）")
    return hard_default


def claim_reward(server: str, token: str, proxies: Dict[str, str] | None,
                 mission_id: int, label: str) -> Tuple[bool, str]:
    """通用领奖：POST /api/missions/{id}/rewards {id}"""
    data = api_request(server, f"/api/missions/{mission_id}/rewards", token, proxies, "POST",
                       {"id": mission_id})
    if is_ok(data) or (isinstance(data, dict) and data.get("rewardValue") is not None):
        value = data.get("rewardValue") if isinstance(data, dict) else None
        return True, f"✅{('+' + str(value)) if value is not None else ''}"
    msg = err_msg(data)
    if any(k in msg for k in ("已领", "已领完", "already", "重复")):
        return True, "奖励已领取"
    return False, f"⚠️{msg}"


def do_mission(server: str, token: str, proxies: Dict[str, str] | None,
               code: str, hard_default: int, label: str, env_val: str) -> str:
    """上报完成（records）+ 领取奖励（rewards）"""
    mission_id = resolve_mission_id(server, token, proxies, code, label, env_val, hard_default)
    print(f"🎯 [任务] 【{label}】任务: id={mission_id} code={code}")

    record = api_request(server, EP_MISSION_RECORDS, token, proxies, "POST",
                         {"code": code, "missionId": mission_id})
    if is_ok(record):
        pass
    elif DEBUG_MODE:
        print(f"  🐞 [任务] 【{label}】上报返回: {err_msg(record)}")

    ok, detail = claim_reward(server, token, proxies, mission_id, label)
    return f"{label}{detail}"


def do_sign(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """每日签到：成功=2xx 无 code；重复签到=HTTP 400 {"code":"already_signed"}"""
    data = api_request(server, EP_SIGN, token, proxies, "POST", {})
    if is_ok(data):
        rewards = data.get("rewards") or []
        gained = sum(int(r.get("count") or 0) for r in rewards if isinstance(r, dict))
        return {"signed": True, "gained": gained}

    code = str(data.get("code") or "")
    if code == "already_signed" or "已签" in str(data.get("message") or ""):
        return {"signed": True, "already": True}
    return {"signed": False, "err": err_msg(data)}


def do_missions(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """互动任务：浏览上新（records+rewards）+ 收藏商品（收藏/取消+rewards）"""
    details: List[str] = []

    # 1) 浏览上新：上报完成 + 领奖
    details.append(do_mission(server, token, proxies,
                              MISSION_CODE_BROWSE_NEW, MISSION_DEFAULT_ID_BROWSE_NEW,
                              "浏览上新", ZIPPO_BROWSE_NEW_ID))

    sleep(random.randint(1, 3))

    # 2) 收藏商品：收藏一个 sku -> 取消收藏 -> 领收藏任务奖励（收藏动作自动触发 records）
    sku_id = None
    goods = api_request(server, EP_GOODS_SEARCH, token, proxies, "POST",
                        {"page": 1, "perPage": 20, "catalogIds": [], "q": "", "sort": "",
                         "properties": "", "priceRange": []})
    goods_list = None
    if isinstance(goods, dict):
        for key in ("list", "data", "records", "items"):
            if isinstance(goods.get(key), list):
                goods_list = goods[key]
                break
        if goods_list is None and isinstance(goods, list):
            goods_list = goods
    if isinstance(goods, list):
        goods_list = goods

    if isinstance(goods_list, list):
        for item in goods_list:
            if not isinstance(item, dict):
                continue
            skus = item.get("skus") or []
            sku_id = (skus[0] or {}).get("id") if skus and isinstance(skus[0], dict) else None
            sku_id = sku_id or item.get("skuId")
            if sku_id:
                break

    if not sku_id:
        details.append("收藏商品⚠️未取到商品/skuId")
        return {"done": True, "details": "、".join(details)}

    api_request(server, EP_FAVORITES, token, proxies, "POST",
                {"targetType": "sku", "targetId": sku_id, "favorited": True})
    sleep(random.randint(1, 2))
    api_request(server, EP_FAVORITES, token, proxies, "POST",
                {"targetType": "sku", "targetId": sku_id, "favorited": False})

    fav_id = resolve_mission_id(server, token, proxies,
                                MISSION_CODE_FAVORITE, "收藏商品",
                                ZIPPO_FAVORITE_ID, MISSION_DEFAULT_ID_FAVORITE)
    ok, detail = claim_reward(server, token, proxies, fav_id, "收藏商品")
    details.append(f"收藏商品{detail}")

    return {"done": True, "details": "、".join(details)}


def query_points(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    data = api_request(server, f"{EP_POINTS}?withoutList=1", token, proxies, "GET")
    if isinstance(data, dict) and data.get("balance") is not None:
        return str(data["balance"])
    return "-"

def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "missionMsg": "-",
        "points": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login, 300)}"
        return result

    result["token"] = mask(token)

    try:
        profile = api_request(server, EP_USER, token, proxies, "GET")
        if is_ok(profile):
            level = profile.get("memberLevel") or profile.get("level") or "-"
            nickname = profile.get("nickname") or "-"
            print(f"👤 {nickname} | 会员等级: {level}")

        sleep(random.randint(1, 3))

        sign_res = do_sign(server, token, proxies)
        if sign_res.get("signed"):
            if sign_res.get("already"):
                result["signMsg"] = "今日已签到"
            else:
                gained = sign_res.get("gained") or 0
                result["signMsg"] = f"签到成功{'，获得' + str(gained) + '分' if gained else ''}"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = f"签到失败: {sign_res.get('err')}"
            print(f"❌ [签到] {result['signMsg']}")

        sleep(random.randint(1, 3))

        mission_res = do_missions(server, token, proxies)
        result["missionMsg"] = mission_res.get("details") or "-"
        print(f"🎯 [任务] {result['missionMsg']}")

        sleep(random.randint(1, 2))

        result["points"] = query_points(server, token, proxies)
        print(f"💰 [积分] 当前积分: {result['points']}")

        result["success"] = bool(sign_res.get("signed"))
        if not result["success"]:
            result["error"] = result["signMsg"]
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🔥 zippo会员 code 版任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
📝 签到：{res["signMsg"]}
🎯 任务：{res["missionMsg"]}
💰 积分：{res["points"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    results = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "missionMsg": "-",
                "points": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    # 失败原因在控制台也输出一份（推送未配置时用户仍能看到诊断信息）
    for idx, res in enumerate(results, 1):
        if not res["success"] and res.get("error"):
            print(f"\n❌ [账号 {idx}] {res['server']} 失败原因：")
            for line in str(res["error"]).splitlines():
                print("   " + line)

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 zippo会员任务执行完成                     ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🔥 zippo会员任务完成", build_notify(results))


if __name__ == "__main__":
    main()
