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
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
爱隐形小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /sso/login/baselogin 使用 code 换 userid
  3. 每日签到（getsignlist 查询 + sign-in 提交）
  4. 查询金币资产
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       覆盖本地 code 服务地址，可选

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对
   （源脚本为 userid 抓包型，/sso/login/baselogin 的 code 参数位为推断）
"""


import hashlib
import json
import os
import random
import string
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "爱隐形小程序"
APPID = "wx0a7972d739462c46"

SERVERS = [
    "10.30.9.183:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://japi.yinxingyanjing.com"
LOGIN_URL = f"{BASE_URL}/sso/login/baselogin"
# 2026-09-15 新抓包坐实：wx.login code -> user_id 的真实入口
MINALOGIN_URL = f"{BASE_URL}/sso/login/minalogin"
OPENID_URL = f"{BASE_URL}/open/weixin/getminaopenid"
SIGN_LIST_URL = f"{BASE_URL}/user/sigouser/getsignlist"
SIGN_IN_URL = f"{BASE_URL}/user/sigouser/sign-in"
# 抓包得到的 UserId（DES 加密串，形如 xxx==）。设置后直接用它登录，无需 code。
YXYJ_USERID = os.getenv("YXYJ_USERID", "")
SIGNATURE = os.getenv("YXYJ_SIGNATURE", "0CC1101A78FF0BB6A86F762333EA978F")
ASSET_URL = f"{BASE_URL}/user/user-info/getassetheaderinfo"

# 源脚本内置的签名 token（所有请求的 signature 计算都要用它）
SIGN_TOKEN = "4224D9FF108FE2BAB4B6F30964839B94"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aiyxcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003229) "
    "NetType/WIFI MiniProgramEnv/iPhone"
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
    print("║ 👓 爱隐形小程序动态 code 版                  ║")
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


def make_nonce(length: int = 16) -> str:
    chars = string.digits + string.ascii_letters
    return "".join(random.choice(chars) for _ in range(length))


def common_headers(token: str | None = None) -> Dict[str, str]:
    """爱隐形请求头：身份在 payload 的 user_id 中，token 参数仅保留模板签名"""
    timestamp = str(int(time.time()))
    noncestr = make_nonce()
    sign_raw = f"noncestr={noncestr}&timestamp={timestamp}&token={SIGN_TOKEN}"
    signature = hashlib.md5(sign_raw.encode("utf-8")).hexdigest().upper()

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/173/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "timestamp": timestamp,
        "noncestr": noncestr,
        "signature": signature,
        "sourcechannel": "Mina",
        "version": "2.0.1",
    }
    return headers


def extract_token(data: Any) -> str | None:
    """爱隐形以 userid 作为业务身份标识，从登录响应中提取 userid"""
    if not isinstance(data, dict):
        return None

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}

    candidates = []
    for obj in (data, inner):
        for key in ("UserId", "userId", "userid", "user_id", "id"):
            value = obj.get(key)
            if value not in (None, "", "null", 0):
                candidates.append(value)

    for item in candidates:
        if item:
            return str(item)

    return None


def login_by_userid(server: str, user_id: str, proxies: Dict[str, str] | None) -> Dict[str, Any] | None:
    """用抓包得到的 UserId 直接登录（HAR 坐实：baselogin 接受 UserId）"""
    try:
        data = api_post(server, LOGIN_URL, "", proxies,
                        {"sourceChannel": "Mina", "UserId": user_id, "login_type": "MemberCenter"})
        return data.get("data") if data.get("data") else None
    except Exception:
        return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """wx.login code -> user_id（2026-09-15 新抓包坐实）。

    真实链路（HAR 证据）：
      1) POST /sso/login/minalogin  {"sourceChannel":"Mina","code":"<wx.login code>"}
         -> {"union_id":..., "open_id":..., "user_id":"<base64>", "error_code":""}
      2) POST /sso/login/baselogin {"sourceChannel":"Mina","login_type":"Mina","UserId":<user_id>}
         -> 校验并拿用户资料（金币等）
    """
    try:
        print("🔐 [登录] 使用 code 换 user_id (/sso/login/minalogin)")
        response = request_with_proxy(
            "POST", MINALOGIN_URL,
            headers=common_headers(),
            json={"sourceChannel": "Mina", "code": code},
            proxies=proxies, server=server,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        user_id = str(data.get("user_id") or "")
        if not user_id:
            print(f"❌ [登录] code 换 user_id 失败: {data.get('error_code') or json_preview(data)}")
            return None, data

        print(f"✅ [登录] user_id 获取成功: {mask(user_id)}")
        # 用 baselogin 校验并取资料（HAR：login_type=Mina）
        try:
            probe = api_post(server, LOGIN_URL, user_id, proxies,
                             {"sourceChannel": "Mina", "login_type": "Mina", "UserId": user_id})
            if isinstance(probe, dict) and probe.get("data"):
                d = probe.get("data") or {}
                print(f"👤 [登录] {d.get('phone') or '-'} | 金币 {d.get('gold', '-')}")
            return user_id, data
        except Exception as exc:
            print(f"⚠️ [登录] baselogin 校验异常（不影响 user_id）: {exc}")
            return user_id, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None

def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "error_code": -1,
            "error_msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "error_code": -1,
            "error_msg": f"JSON解析失败: {response.text[:300]}",
        }


# ====================== Token缓存管理 ======================
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
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 userid（金币资产接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 userid")
        try:
            asset_resp = api_post(server, ASSET_URL, cache_token, proxies, {"user_id": cache_token})
            if not asset_resp.get("error_code"):
                print("✅ [缓存] userid 有效")
                return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] userid 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, raw_login

    expire_time = None
    if raw_login and isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "goldMsg": "-",
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

    # 优先：抓包得到的 UserId（抓包坐实：baselogin 用 UserId 即可登录取到真实用户）
    if YXYJ_USERID:
        print("🔍 [兜底] 使用抓包 UserId 验证")
        probe = login_by_userid(server, YXYJ_USERID, proxies)
        if probe:
            print(f"✅ [兜底] UserId 有效 | {probe.get('phone')} | 金币 {probe.get('gold')}")
            result["token"] = mask(YXYJ_USERID)
            result["userMsg"] = f"{probe.get('phone') or '-'} (金币 {probe.get('gold')})"
            try:
                slist = api_post(server, SIGN_LIST_URL, YXYJ_USERID, proxies,
                                 {"sourcechannel": "Mina", "user_id": YXYJ_USERID})
                rules = safe_data(slist).get("signRules") or []
                result["signMsg"] = f"签到规则 {len(rules)} 条（已加载）"
                print(f"📋 [签到] {result['signMsg']}")
                result["success"] = True
                return result
            except Exception as exc:
                result["error"] = f"UserId 模式执行异常: {exc}"
                return result
        print("⚠️ [兜底] UserId 无效，回退 code 登录")

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    if raw_login and isinstance(raw_login, dict):
        username = safe_data(raw_login).get("username", "未知")
        print(f"👤 [账号] {username}")

    try:
        sign_list_resp = api_post(
            server,
            SIGN_LIST_URL,
            token,
            proxies,
            {"sourcechannel": "Mina", "user_id": token},
        )
        if sign_list_resp.get("error_code"):
            result["error"] = f"获取签到列表失败: {sign_list_resp.get('error_msg')}"
            print(f"❌ [签到] {result['error']}")
            return result

        today_info = safe_data(sign_list_resp).get("todaySignInfo") or {}
        is_sign = today_info.get("isSign")
        sign_msg = today_info.get("msg", "") or ""
        print(f"📋 [签到] 今日{'已' if is_sign == 1 else '未'}签到 {sign_msg}".strip())

        if is_sign == 0:
            wait_time = random.randint(2, 5)
            print(f"⏳ [签到] 提交前等待 {wait_time}s")
            sleep(wait_time)

            sign_resp = api_post(
                server,
                SIGN_IN_URL,
                token,
                proxies,
                {"user_id": token, "source_channel": "Mina"},
            )
            if sign_resp.get("error_code"):
                result["signMsg"] = f"签到执行失败: {sign_resp.get('error_msg')}"
                print(f"❌ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = f"签到成功 {sign_msg}".strip()
                print(f"✅ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = f"今日已签到 {sign_msg}".strip()
            print(f"✅ [签到] {result['signMsg']}")

        asset_resp = api_post(server, ASSET_URL, token, proxies, {"user_id": token})
        if not asset_resp.get("error_code"):
            gold = safe_data(asset_resp).get("gold", "未知")
            result["goldMsg"] = str(gold)
            print(f"💰 [金币] 当前总金币: {gold}")
        else:
            result["goldMsg"] = f"获取失败: {asset_resp.get('error_msg')}"
            print(f"⚠️ [金币] {result['goldMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""👓 爱隐形小程序任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
💰 金币：{res["goldMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    results: List[Dict[str, Any]] = []

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
                "goldMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 爱隐形任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("👓 爱隐形任务完成", build_notify(results))


if __name__ == "__main__":
    main()
