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
# 功能说明：顺丰速运+ 中秋博饼集礼盒 code 版（对齐铛铛一下.py 的 code 接口）
# 机制：code 接口获取微信 code → UCMP appOnLogin 换 sessionId
#       → sfnewactivity 换顺丰 Cookie 凭证 → 缓存到本地 JSON；
#       下次运行先读取缓存 Cookie 并验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效则重新获取 code 自动刷新。
# ==========================================================

# 顺丰速运+ 中秋博饼集礼盒活动（MID_AUTUMN_2026）
#
# 功能：
#   1. code 接口获取微信 code（对齐铛铛一下.py）
#   2. code 换顺丰 Cookie 凭证（UCMP appOnLogin + 会员绑定 + sfnewactivity）
#   3. 进入活动页（带邀请互刷）→ 每日礼包
#   4. 小程序 + APP 双渠道任务分别做、分别领
#   5. 博饼（每日3次，失败自动重试）→ 博饼后补领游戏任务奖励
#   6. 集礼盒（消耗次数集碎片）→ 开礼盒抽奖 / 周四抽奖
#   7. 品赞代理，业务请求优先代理，失败直连兜底
#   8. PushPlus + 企业微信机器人 推送
#
# 环境变量：
#   PLUSPLUS_TOKEN    PushPlus token，可选
#   QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
#   PROXY_API         品赞代理提取 API，可选
#   PROXY_TYPE        http / socks5，默认 http
#   SF_DRY_RUN        true 时仅查询不消耗（自检模式）
#   SF_CHANNEL        mp / app，只跑单渠道（默认双渠道都跑）
#   SF_VERBOSE        true 时详细日志
#   SF_SUBSCRIBE      true 时开启每日礼包订阅（默认关闭）
#   SF_INVITE_TYPE    邀请参数类型（中秋默认1）
#   SF_COOKIE         手动 Cookie 兜底（可选，sfsyUrl 同格式）
#   CODE_SERVER       覆盖 code 服务地址，可选
#
# 依赖：
#   pip install requests
#   socks5 代理需：pip install requests[socks]

import hashlib
import json
import os
import random
import re
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote, urlparse, parse_qs

import requests


APP_NAME = "顺丰中秋博饼"
APPID = "wxd4185d00bf7e08ac"
SF_PUBLIC_ID = "gh_f9d9fca26a50"
SF_OAUTH_APPID = "wx0d9aa0e894066e87"
SF_OAUTH_SCENE = "692"
UCMP_BASE = "https://ucmp.sf-express.com"
MCS_BASE = "https://mcs-mimp-web.sf-express.com"

# code 接口服务（对齐铛铛一下.py 的 SERVERS）
SERVERS = [
    "10.30.9.183:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
FALLBACK_COOKIE = os.getenv("SF_COOKIE", "")

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf254173b) XWEB/19027"
)

inviteId = ['B9D26F54D0684410B133577D5F9B565D', '787450A526F644BCAEAF53D3A928B034',
            '6FB4C632AE034028A36E45D976FF40A7', '1F3180464F444406A4DE0B6F4D84E841',
            'C365C74E0B0844FB8C78656FF8375ECD', 'E5844EF6D2E34B2BB11CFABF13FDB2B4',
            'D78098B2D29A496CA447D39140C0377F', '7054A99B93194C90A0B2A9C11A9322E0',
            'FC9A7128E1AE4E419E385FF0DF91B799']

print_lock = Lock()

# ===== 兼容原脚本的模块级常量 =====
SF_WX_APPID = APPID
ENABLE_PROXY = os.getenv("ENABLE_PROXY", "false").lower() == "true"
MAX_PROXY_RETRIES = 5
PROXY_TIMEOUT = 15
REQUEST_RETRY_COUNT = 3
SUBSCRIBE_CODE = "MID_AUTUMN_2026_DAILY_BAIWAN"
BOBING_RANK_CN = {
    1: "状元", 2: "榜眼", 3: "探花", 4: "进士", 5: "举人", 6: "秀才", 0: "参与奖",
}
SKIP_TASK_TYPES = [
    "SEND_SUCCESS_RECALL",
    "LOOK_BIG_PACKAGE_GET_CASH",
    "OPEN_FAMILY_HOME_MUTUAL",
    "SHUNYUN_CARD",
    "CHARGE_NEW_EXPRESS_CARD",
    "OPEN_APP_NOTIFICATION",
]

# ===== 中秋博饼集礼盒活动配置 =====
ACTIVITY_CODE = "MID_AUTUMN_2026"
CHANNEL = "26zhongqiu07"
CHANNEL_TYPE = "MINI_PROGRAM"
CITY_CODE = "551"
TOKEN = "wwesldfs29aniversaryvdld29"
SYS_CODE = "MCS-MIMP-CORE"
INVITE_TYPE = int(os.getenv("SF_INVITE_TYPE", "1"))
CHANNEL_FILTER = os.getenv("SF_CHANNEL", "mp,app").lower()
DRY_RUN = os.getenv("SF_DRY_RUN", "false").lower() == "true"
VERBOSE = os.getenv("SF_VERBOSE", "false").lower() == "true"
ENABLE_SUBSCRIBE = os.getenv("SF_SUBSCRIBE", "false").lower() == "true"
RESULT_FILE = os.getenv("SF_RESULT_FILE", "sf_midautumn_results.jsonl")

CHANNELS = [
    {"name": "小程序", "channel": "26zhongqiu07", "channelType": "MINI_PROGRAM", "platform": "MINI_PROGRAM"},
    {"name": "APP", "channel": "26zhongqiu01", "channelType": "SFAPP", "platform": "SFAPP"},
]
CHANNELS = [c for c in CHANNELS
            if (c["channelType"] == "MINI_PROGRAM" and "mp" in CHANNEL_FILTER)
            or (c["channelType"] == "SFAPP" and "app" in CHANNEL_FILTER)]


def mask_mobile(mobile: str) -> str:
    mobile = str(mobile or "")
    if len(mobile) >= 11:
        return mobile[:3] + "****" + mobile[-4:]
    return mobile

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
    print("║ 🥮 顺丰中秋博饼 code 版                      ║")
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
    """顺丰业务请求头（照 顺丰中秋.py 原实现）"""
    timestamp = str(int(round(time.time() * 1000)))
    sign_raw = f"token={TOKEN}&timestamp={timestamp}&sysCode={SYS_CODE}"
    signature = hashlib.md5(sign_raw.encode()).hexdigest()
    headers = {
        "Host": "mcs-mimp-web.sf-express.com",
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "channel": CHANNEL,
        "platform": "MINI_PROGRAM",
        "accept-language": "zh-CN,zh;q=0.9",
        "syscode": SYS_CODE,
        "timestamp": timestamp,
        "signature": signature,
    }
    if token:
        headers["token"] = token
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
    if isinstance(data, dict) and data.get("cookie"):
        return data["cookie"]
    return None


def set_cached_token(server: str, cookie: str) -> None:
    cache = load_token_cache()
    cache[server] = {"cookie": cookie, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)

# ========== code 换顺丰 Cookie 凭证（照 sf.py 已验证实现） ==========
def _ucmp_app_on_login(code: str) -> Optional[Dict]:
    try:
        url = f"{UCMP_BASE}/wxaccess/weixin/appOnLogin"
        r = direct_session().get(url, params={"code": code, "publicId": SF_PUBLIC_ID}, timeout=25)
        j = r.json()
        if j.get("sessionId") and j.get("openid"):
            return j
        return None
    except Exception:
        return None


def exchange_cookie_by_code(code: str, proxies: Dict[str, str] | None, server: str) -> str | None:
    """通过本地 code 服务拿到的 code，走 UCMP 换取顺丰 Cookie 凭证。"""
    ucmp = _ucmp_app_on_login(code)
    if not ucmp:
        print(f"❌ [登录] {server} appOnLogin 失败")
        return None

    suuid = ucmp.get("sessionId", "")
    if not suuid:
        print(f"❌ [登录] {server} appOnLogin 未返回 sessionId")
        return None

    try:
        s = requests.Session()
        s.verify = False
        ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_2 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "MicroMessenger/8.0.69(0x1800452d) NetType/WIFI Language/zh_CN"
        )

        # —— 对齐 sf_发帖版.py 的转化方式：先完成微信↔顺丰会员绑定 ——
        # 关键一步：appOnLogin 之后、sfnewactivity 之前，调用 wxMemIsBind 完成
        # 会员绑定握手。缺这一步时 sfnewactivity 只会产生“看似登录”的会话
        # （有 sessionId/手机号），但会员绑定未完成，业务接口会报
        # “用户信息失效，请退出重新进入”。这一步是 sf_发帖版.py 能成功、
        # 而旧 sfsy.py 失败的核心差异。
        try:
            bind_headers = {
                "user-agent": ua,
                "content-type": "application/json",
                "accept": "application/json, text/plain, */*",
                "cookie": f"suuid={suuid}",
                "referer": f"https://servicewechat.com/{SF_WX_APPID}/663/page-frame.html",
            }
            s.post(
                f"{UCMP_BASE}/wxopen/weixin/wxMemIsBind",
                json={},
                headers=bind_headers,
                timeout=15,
                proxies=proxies,
            )
        except Exception:
            # 绑定查询失败不阻断，后续仍按 Cookie 取手机号
            pass

        biz_code = json.dumps({
            "path": "/up-member/newPoints",
            "linkCode": "SFAC20230803190840424",
            "supportShare": "YES",
            "subCategoryCode": "1",
            "from": "mypoint",
            "categoryCode": "1",
        }, ensure_ascii=False)
        sfnew_url = (
            f"{UCMP_BASE}/wechat-act/weixin/activity/sfnewactivity?"
            f"bizCode={quote(biz_code)}&regSource=mypoint&citycode=025"
            f"&cityname={quote('广州')}&wxapp-version=V17.49&suuid={suuid}"
        )
        sfnew_headers = {
            "user-agent": ua,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        s.get(sfnew_url, headers=sfnew_headers, timeout=25, allow_redirects=True, proxies=proxies)

        cookies: Dict[str, str] = {}
        for c in s.cookies:
            if "mcs-mimp" in c.domain or "sf-express" in c.domain:
                cookies[c.name] = c.value

        session_id = cookies.get("sessionId") or s.cookies.get("sessionId", "")
        login_mobile = cookies.get("_login_mobile_") or s.cookies.get("_login_mobile_", "")
        login_user_id = cookies.get("_login_user_id_") or s.cookies.get("_login_user_id_", "")

        # 兜底：部分环境下需要再访问会员页补齐 cookie（含 WAF 凭证）
        if session_id and (not login_mobile or not login_user_id):
            try:
                s.headers.update({
                    "User-Agent": ua,
                    "Cookie": f"sessionId={session_id}",
                })
                s.get(
                    f"{MCS_BASE}/mcs-mimp/app/index.html",
                    allow_redirects=True,
                    timeout=15,
                    proxies=proxies,
                )
                for c in s.cookies:
                    if "mcs-mimp" in c.domain or "sf-express" in c.domain:
                        cookies[c.name] = c.value
                login_mobile = cookies.get("_login_mobile_", "")
                login_user_id = cookies.get("_login_user_id_", "")
                session_id = cookies.get("sessionId", session_id)
            except Exception as exc:
                print(f"⚠️ [登录] {server} 访问会员页补充凭证异常: {exc}")

        if not session_id or not login_mobile or not login_user_id:
            print(
                f"❌ [登录] {server} Cookie 不完整 session={bool(session_id)} "
                f"mobile={bool(login_mobile)} uid={bool(login_user_id)}"
            )
            return None

        # 对齐 sf_发帖版.py：只拼接业务必需的凭证（sessionId / 手机号 / uid
        # + 可选的 WAF / JSESSIONID），不夹带其它临时 token，避免干扰会话判定。
        parts = [
            f"sessionId={session_id}",
            f"_login_mobile_={login_mobile}",
            f"_login_user_id_={login_user_id}",
        ]
        for k in ("HWWAFSESTIME", "HWWAFSESID", "JSESSIONID"):
            if cookies.get(k):
                parts.append(f"{k}={cookies[k]}")

        all_cookies = {}
        for p in parts:
            k, _, v = p.partition("=")
            all_cookies[k] = v
        waf_ok = all(k in all_cookies for k in ("HWWAFSESID", "HWWAFSESTIME"))
        print(
            f"🍪 [登录] {server} 已捕获凭证: "
            f"{','.join(sorted(all_cookies.keys()))}  WAF={'✅' if waf_ok else '❌'}"
        )
        cookie_str = ";".join(parts)
        print(f"✅ [登录] {server} 凭证换绑成功 ➔ 手机: {mask_mobile(login_mobile)}")
        return cookie_str
    except Exception as exc:
        print(f"❌ [登录] {server} 换取 Cookie 异常: {exc}")
        return None


class ProxyManager:
    """兼容原脚本接口的代理管理器（实际代理走 request_with_proxy/品赞框架）"""

    def __init__(self, api_url: str = ""):
        self.api_url = api_url

    def get_proxy(self) -> Optional[Dict[str, str]]:
        return None


# ========== 顺丰业务 HTTP 客户端与活动执行器（照 顺丰中秋.py 原实现） ==========
class Logger:
    def __init__(self, verbose: bool = False):
        self.messages: List[str] = []
        self.lock = Lock()
        self.verbose = verbose

    def _log(self, icon: str, msg: str):
        line = f"{icon} {msg}"
        with print_lock:
            print(line)
        with self.lock:
            self.messages.append(line)

    def info(self, msg): self._log('📝', msg)
    def success(self, msg): self._log('✅', msg)
    def warning(self, msg): self._log('⚠️', msg)
    def error(self, msg): self._log('❌', msg)
    def task(self, msg): self._log('🎯', msg)
    def medal(self, msg): self._log('🏅', msg)
    def dice(self, msg): self._log('🎲', msg)

    def detail(self, msg):
        """详细日志：仅 SF_VERBOSE=true 时输出"""
        if self.verbose:
            self._log('📄', msg)


class SFHttpClient:
    def __init__(self, proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager
        self.session = requests.Session()
        self.session.verify = False

        if ENABLE_PROXY:
            proxy = self.proxy_manager.get_proxy()
            if proxy:
                self.session.proxies = proxy
            else:
                if self.proxy_manager.api_url:
                    print("⚠️ 代理获取失败，将不使用代理")

        self.headers = {
            'Host': 'mcs-mimp-web.sf-express.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'channel': CHANNEL,
            'platform': 'MINI_PROGRAM',
            'accept-language': 'zh-CN,zh;q=0.9',
        }

    def set_channel(self, channel: str, platform: str):
        """切换渠道请求头（小程序/APP 任务不同）"""
        self.headers['channel'] = channel
        self.headers['platform'] = platform

    def _generate_sign(self) -> Dict[str, str]:
        timestamp = str(int(round(time.time() * 1000)))
        data = f'token={TOKEN}&timestamp={timestamp}&sysCode={SYS_CODE}'
        signature = hashlib.md5(data.encode()).hexdigest()
        return {
            'syscode': SYS_CODE,
            'timestamp': timestamp,
            'signature': signature,
        }

    def request(self, url: str, data: Optional[Dict] = None, method: str = 'POST') -> Optional[Dict]:
        retry_count = 0
        max_proxy_retries = MAX_PROXY_RETRIES if ENABLE_PROXY else 1
        proxy_retry_count = 0

        while proxy_retry_count < max_proxy_retries:
            sign_data = self._generate_sign()
            headers = {**self.headers, **sign_data}

            try:
                if method == 'POST':
                    resp = self.session.post(url, headers=headers, json=data or {}, timeout=PROXY_TIMEOUT)
                else:
                    resp = self.session.get(url, headers=headers, timeout=PROXY_TIMEOUT)
                resp.raise_for_status()

                try:
                    result = resp.json()
                    if result is None:
                        retry_count += 1
                        if retry_count < REQUEST_RETRY_COUNT:
                            time.sleep(2)
                            continue
                        return None
                    return result
                except (json.JSONDecodeError, ValueError):
                    retry_count += 1
                    if retry_count < REQUEST_RETRY_COUNT:
                        time.sleep(2)
                        continue
                    return None

            except requests.exceptions.RequestException as e:
                retry_count += 1
                error_str = str(e)

                if ENABLE_PROXY and ('ProxyError' in error_str or 'SSLError' in error_str or 'ConnectionError' in error_str):
                    proxy_retry_count += 1
                    if proxy_retry_count < MAX_PROXY_RETRIES:
                        new_proxy = self.proxy_manager.get_proxy()
                        if new_proxy:
                            self.session.proxies = new_proxy
                        retry_count = 0
                    time.sleep(2)
                    continue

                if retry_count < REQUEST_RETRY_COUNT:
                    time.sleep(2)
                    continue
                return None

            except Exception:
                return None

        return None

    def login(self, url: str) -> tuple:
        """登录（兼容URL和CK格式）"""
        try:
            decoded_input = unquote(url)
            if decoded_input.startswith('sessionId=') or '_login_mobile_=' in decoded_input:
                cookie_dict = {}
                for item in decoded_input.split(';'):
                    item = item.strip()
                    if '=' in item:
                        k, v = item.split('=', 1)
                        cookie_dict[k] = v
                for k, v in cookie_dict.items():
                    self.session.cookies.set(k, v, domain='mcs-mimp-web.sf-express.com')
                user_id = cookie_dict.get('_login_user_id_', '')
                phone = cookie_dict.get('_login_mobile_', '')
                return (True, user_id, phone) if phone else (False, '', '')
            else:
                self.session.get(unquote(url), headers=self.headers, timeout=PROXY_TIMEOUT)
                cookies = self.session.cookies.get_dict()
                user_id = cookies.get('_login_user_id_', '')
                phone = cookies.get('_login_mobile_', '')
                return (True, user_id, phone) if phone else (False, '', '')
        except Exception as e:
            print(f'登录异常: {str(e)}')
            return False, '', ''


class MidAutumnExecutor:
    def __init__(self, http: SFHttpClient, logger: Logger, user_id: str, dry_run: bool = False, inviter_id: str = ''):
        self.http = http
        self.logger = logger
        self.user_id = user_id
        self.dry_run = dry_run
        self.inviter_id = inviter_id
        # 当前渠道状态（默认小程序；双渠道任务阶段会切换）
        self.channel = CHANNEL
        self.channel_type = CHANNEL_TYPE
        self.channel_name = '小程序'
        self.channel_key = 'mp'

    def _use_channel(self, cfg: Dict) -> None:
        """切换渠道：请求头 + 任务/领奖参数 + 统计键"""
        self.channel = cfg['channel']
        self.channel_type = cfg['channelType']
        self.channel_name = cfg['name']
        self.channel_key = 'mp' if cfg['channelType'] == 'MINI_PROGRAM' else 'app'
        self.http.set_channel(cfg['channel'], cfg['platform'])

    def _count_task(self, result: Dict) -> None:
        """任务计数：总计 + 当前渠道计数"""
        result['tasks_completed'] = result.get('tasks_completed', 0) + 1
        result[f'tasks_{self.channel_key}'] = result.get(f'tasks_{self.channel_key}', 0) + 1

    # ---------- 通用请求封装 ----------
    def _post(self, url: str, data: Optional[Dict] = None) -> Optional[Dict]:
        resp = self.http.request(url, data=data or {})
        if resp and resp.get('success'):
            return resp.get('obj')
        return None

    def _post_full(self, url: str, data: Optional[Dict] = None) -> Optional[Dict]:
        return self.http.request(url, data=data or {})

    @staticmethod
    def _err(resp: Optional[Dict]) -> str:
        return resp.get('errorMessage', '未知错误') if resp else '请求失败'

    def _consume(self, action: str) -> bool:
        """自检模式：消耗型操作统一跳过"""
        if self.dry_run:
            self.logger.info(f'[自检模式] 跳过消耗操作: {action}')
            return False
        return True

    # ---------- 首页 / 邀请 ----------
    def get_activity_index(self, invite_type: int = 0, invite_user_id: str = '', no_login: bool = False) -> Optional[Dict]:
        """进入活动首页；带邀请参数时注册"被邀请访问"关系（no_login 走未登录接口，用于首次访问场景）"""
        if no_login:
            url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonNoLoginPost/~memberNonactivity~midAutumn2026IndexService~index'
        else:
            url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026IndexService~index'
        if invite_type > 0 and invite_user_id:
            data = {"inviteType": invite_type, "inviteUserId": invite_user_id}
        else:
            data = {}
        resp = self._post_full(url, data)
        return resp.get('obj') if resp and resp.get('success') else None

    def _pick_inviter(self) -> str:
        """选择邀请人ID：优先主程序轮转分配的账号，否则从固定邀请池随机"""
        if self.inviter_id:
            return self.inviter_id
        available_invites = [inv for inv in inviteId if inv != self.user_id]
        return random.choice(available_invites) if available_invites else ''

    def get_invite_list(self) -> Optional[List[Dict]]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026TaskService~taskInviteList'
        resp = self._post_full(url)
        if resp and resp.get('success'):
            return resp.get('obj', [])
        return None

    # ---------- 查询类（进入页面触发） ----------
    def is_activity_subscribe(self) -> bool:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~commonSubscribeService~isSubscribe'
        resp = self._post_full(url, {"code": SUBSCRIBE_CODE})
        if resp and resp.get('success'):
            return resp.get('obj', {}).get('subscribe', False)
        return False

    def do_subscribe(self) -> bool:
        """订阅每日礼包提醒（未订阅时补订阅）"""
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~commonSubscribeService~subscribe'
        resp = self._post_full(url, {"code": SUBSCRIBE_CODE})
        if resp and resp.get('success'):
            self.logger.success('每日礼包订阅成功')
            return True
        self.logger.warning(f'每日礼包订阅失败: {self._err(resp)}')
        return False

    def get_dilate_change(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026DilateService~getDilateChange'
        return self._post(url)

    def get_widget_status(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026DilateService~getWidgetStatus'
        return self._post(url)

    def get_dilate_status(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026DilateService~getDilateStatus'
        return self._post(url)

    def get_express_activity_info(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026ExpressService~getExpressSpecialActivityInfo'
        return self._post(url)

    def query_extra_reward_cards(self) -> Optional[List]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026CollectService~queryExtraRewardCards'
        return self._post(url)

    def query_shunyun_recommend(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026ShunYunCardService~queryRecommend'
        return self._post(url)

    def query_emp_gift_detail(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026EmpGiftService~queryDetail'
        return self._post(url)

    def query_family_status(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026FamilyService~familyStatus'
        return self._post(url)

    # ---------- 每日礼包 ----------
    def get_daily_gift_status(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026DailyService~getDailyGiftStatus'
        data = {"cityCode": CITY_CODE, "channel": self.channel}
        resp = self._post_full(url, data)
        return resp.get('obj') if resp and resp.get('success') else None

    def receive_daily_gift(self) -> Optional[Dict]:
        if not self._consume('领取每日礼包'):
            return None
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026DailyService~receiveDailyGift'
        data = {"cityCode": CITY_CODE, "channel": self.channel}
        resp = self._post_full(url, data)
        if resp and resp.get('success'):
            return resp.get('obj')
        else:
            self.logger.warning(f'领取每日礼包失败: {self._err(resp)}')
            return None

    def do_daily_gift(self, result: Dict) -> None:
        time.sleep(1)
        gift = self.get_daily_gift_status()
        if not gift:
            self.logger.warning('每日礼包: 状态获取失败')
            return
        if gift.get('received'):
            self.logger.info('每日礼包: 今日已领取')
            return
        if gift.get('canReceive'):
            time.sleep(1)
            received = self.receive_daily_gift()
            if received:
                products = received.get('dailyGiftProductList', [])
                if products:
                    names = [p.get('productName', '未知') for p in products]
                    result['daily_gift_count'] = len(names)
                    if self.logger.verbose:
                        self.logger.success('每日礼包领取成功: ' + ', '.join(names))
                    else:
                        self.logger.success(f'每日礼包: 领取成功 {len(names)} 张券（{names[0]} 等）')
                else:
                    self.logger.success('每日礼包: 领取成功')
                result['daily_gift_received'] = True
        else:
            self.logger.info('每日礼包: 暂不可领取')

    # ---------- 任务 ----------
    def get_task_list(self) -> Optional[List[Dict]]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~activityTaskService~taskList'
        data = {"activityCode": ACTIVITY_CODE, "channelType": self.channel_type}
        resp = self.http.request(url, data=data)
        if resp and resp.get('success'):
            return resp.get('obj', [])
        else:
            self.logger.error(f'获取任务列表失败: {self._err(resp)}')
            return None

    def get_user_rest_integral(self) -> int:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~activityTaskService~getUserRestIntegral'
        resp = self._post_full(url)
        if resp and resp.get('success'):
            return resp.get('obj', 0)
        return 0

    def finish_task(self, task_code: str) -> bool:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonRoutePost/memberEs/taskRecord/finishTask'
        resp = self.http.request(url, data={"taskCode": task_code})
        return bool(resp and resp.get('success'))

    def check_task(self, task_code: str) -> bool:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonRoutePost/memberEs/taskRecord/checkTask'
        resp = self.http.request(url, data={"taskCode": task_code})
        return bool(resp and resp.get('success'))

    def integral_exchange(self) -> bool:
        """积分兑换集礼盒次数（10积分/次，每日1次）"""
        if not self._consume('积分兑换集礼盒次数'):
            return False
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026TaskService~integralExchange'
        data = {"exchangeNum": 1, "activityCode": ACTIVITY_CODE}
        resp = self._post_full(url, data)
        if resp and resp.get('success'):
            self.logger.detail('积分兑换集礼盒次数成功（消耗10积分）')
            return True
        else:
            self.logger.warning(f'积分兑换失败: {self._err(resp)}')
            return False

    def receive_vip_benefit(self) -> bool:
        """领取寄件券类会员权益（专用接口）"""
        if not self._consume('领取寄件会员权益'):
            return False
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberManage~memberEquity~commonEquityReceive'
        resp = self.http.request(url, data={"key": "surprise_benefit"})
        if resp and resp.get('success'):
            self.logger.detail('[领取寄件会员权益] 完成成功')
            return True
        else:
            self.logger.warning(f'[领取寄件会员权益] 完成失败: {self._err(resp)}')
            return False

    def fetch_task_reward(self) -> Optional[Dict]:
        """领取已完成任务的奖励（给集礼盒次数）"""
        if not self._consume('领取任务奖励'):
            return None
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026TaskService~fetchTaskReward'
        data = {"channelType": self.channel_type, "activityCode": ACTIVITY_CODE}
        resp = self._post_full(url, data)
        if resp and resp.get('success'):
            return resp.get('obj', {})
        else:
            self.logger.warning(f'领取任务奖励失败: {self._err(resp)}')
            return None

    def get_charge_task_reward(self) -> Optional[Dict]:
        """一键集齐本周礼盒奖励（充值任务，空body）"""
        if not self._consume('一键集齐本周礼盒奖励'):
            return None
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026TaskService~getChargeTaskReward'
        resp = self._post_full(url)
        if resp and resp.get('success'):
            obj = resp.get('obj', {})
            received = obj.get('receivedAccountList', [])
            if received:
                for item in received:
                    self.logger.detail(f'一键集齐: 获得 {item.get("currency", "?")} x{item.get("amount", 0)}')
            return obj
        return None

    def _claim_task_rewards(self, result: Dict) -> None:
        """领取当前渠道已完成任务的奖励（给集礼盒次数）"""
        time.sleep(1)
        reward = self.fetch_task_reward()
        gained: List[str] = []
        if reward:
            received_list = reward.get('receivedAccountList', [])
            if received_list:
                for item in received_list:
                    gained.append(f"{item.get('currency', '?')}x{item.get('amount', 0)}")
                result['claim_rewards'] = received_list
        if gained:
            self.logger.success(f'任务奖励[{self.channel_name}]: ' + ', '.join(gained))
        elif self.logger.verbose:
            self.logger.info(f'任务奖励[{self.channel_name}]: 无')

    def do_tasks(self, result: Dict) -> None:
        """执行所有可自动完成的任务"""
        tasks = self.get_task_list()
        if tasks is None:
            return
        self.logger.detail(f'共发现 {len(tasks)} 个任务')

        done_names: List[str] = []
        for task in tasks:
            task_name = task.get('taskName', '未知')
            task_type = task.get('taskType', '')
            task_code = task.get('taskCode', '')
            status = task.get('status')
            process = task.get('process', '')
            rest_finish = task.get('restFinishTime', 0)
            virtual_token = task.get('virtualTokenNum', 0)

            # 已完成
            if status == 3 or (status == 1 and rest_finish <= 0):
                self.logger.detail(f'[{task_name}] 已完成 ({process})')
                continue

            # 邀好友任务：进度靠"被邀请人首次访问"注册（每邀1位+1，需邀满N位），只报进度不执行动作
            if task_type == 'INVITEFRIENDS_PARTAKE_ACTIVITY':
                result['invite_process'] = process
                self.logger.info(f'邀请任务: {process}（邀满{task.get("maxFinishTime", 4)}位好友首次访问，每邀1位+{virtual_token}次）')
                continue

            if task_type in SKIP_TASK_TYPES:
                self.logger.detail(f'[{task_name}] 跳过（需实际操作）')
                continue

            # 玩博饼游戏（通过博饼 draw 完成，稍后统一处理）
            if task_type == 'PLAY_ACTIVITY_GAME':
                self.logger.detail(f'[{task_name}] 通过博饼游戏完成（稍后执行）')
                continue

            # 积分兑换
            if task_type == 'INTEGRAL_EXCHANGE':
                if self.dry_run:
                    self.logger.info(f'[{task_name}] 自检模式，可自动完成')
                    continue
                if self.integral_exchange():
                    self._count_task(result)
                    done_names.append(task_name)
                continue

            # 领取寄件券类会员权益
            if task_type == 'RECEIVE_VIP_BENEFIT':
                if self.dry_run:
                    self.logger.info(f'[{task_name}] 自检模式，可自动完成')
                    continue
                if self.receive_vip_benefit():
                    self._count_task(result)
                    done_names.append(task_name)
                continue

            # 关注类任务（公众号/抖音/小红书）：账号已关注对应平台即可用 taskCode 完成
            if task_type.startswith('FOLLOW_') and task_code:
                if self.dry_run:
                    self.logger.info(f'[{task_name}] 自检模式，可自动完成')
                    continue
                ok = self.finish_task(task_code)
                if not ok:
                    ok = self.check_task(task_code) and self.finish_task(task_code)
                if ok:
                    done_names.append(task_name)
                    self._count_task(result)
                    self.logger.detail(f'[{task_name}] 完成成功，可获得 {virtual_token} 次集礼盒机会')
                else:
                    self.logger.warning(f'[{task_name}] 完成失败（需先在对应平台关注顺丰账号）')
                time.sleep(1)
                continue

            # 有 taskCode 的任务尝试自动完成（浏览类）
            if task_code:
                if self.dry_run:
                    self.logger.info(f'[{task_name}] 自检模式，可自动完成')
                    time.sleep(0.3)
                    continue
                if self.finish_task(task_code):
                    done_names.append(task_name)
                    self._count_task(result)
                    self.logger.detail(f'[{task_name}] 完成成功，可获得 {virtual_token} 次集礼盒机会')
                else:
                    self.logger.warning(f'[{task_name}] 完成失败')
                time.sleep(1)
            else:
                self.logger.detail(f'[{task_name}] 跳过（无taskCode, {task_type}）')

        # 领取任务奖励（给集礼盒次数）
        self._claim_task_rewards(result)

        if done_names:
            self.logger.success(f'任务[{self.channel_name}]: 完成 {len(done_names)} 个（' + '/'.join(done_names) + '）')
        elif self.dry_run:
            self.logger.info(f'任务[{self.channel_name}]: 自检模式，未实际执行（可自动完成的任务已在上方列出）')
        elif self.logger.verbose:
            self.logger.info(f'任务[{self.channel_name}]: 无自动可完成任务')

    # ---------- 博饼游戏 ----------
    def bobing_index(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026BobingService~index'
        return self._post(url)

    def bobing_summary(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026BobingService~summary'
        return self._post(url)

    def bobing_draw(self) -> Optional[Dict]:
        """博饼抽一次（失败自动重试，保证不浪费次数）"""
        if not self._consume('博饼'):
            return None
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026BobingService~draw'
        for attempt in range(REQUEST_RETRY_COUNT):
            resp = self._post_full(url)
            if resp and resp.get('success'):
                return resp.get('obj')
            self.logger.warning(f'博饼失败(第{attempt+1}次): {self._err(resp)}')
            time.sleep(1)
        return None

    def do_bobing(self, result: Dict) -> None:
        """玩博饼游戏（每日免费次数）"""
        summary = self.bobing_summary()
        if not summary:
            self.logger.warning('博饼: 状态获取失败')
            return
        rest = summary.get('restCount', 0)
        daily = summary.get('dailyLimit', 0)
        self.logger.detail(f'博饼: 剩余次数 {rest}/{daily}')

        if rest <= 0:
            self.logger.info('博饼: 今日免费次数已用完')
            return

        detail = result.setdefault('bobing_detail', [])
        rank_list: List[str] = []
        for i in range(rest):
            time.sleep(1)
            draw = self.bobing_draw()
            if not draw:
                self.logger.warning(f'博饼第 {i+1} 次失败，停止')
                break

            dice_list = draw.get('dice', [])
            rank = draw.get('rank', '')
            level = draw.get('level', -1)
            keju = draw.get('keju', '')
            sub_award = draw.get('subAward', '')
            rest_count = draw.get('restCount', 0)

            rank_cn = keju or sub_award or BOBING_RANK_CN.get(level, '') or rank

            # 收集本次奖励名（兼容多种返回字段，确保不漏领）
            rewards: List[str] = []
            for key in ('productList', 'productDTOList', 'couponList', 'giftList', 'awardList'):
                for p in draw.get(key, []) or []:
                    rewards.append(p.get('productName') or p.get('couponName') or p.get('giftBagName') or '未知')
            if not rewards:
                for acc in draw.get('receivedAccountList', []) or []:
                    rewards.append(f"{acc.get('currency', '?')}x{acc.get('amount', 0)}")

            if self.logger.verbose:
                line = f'第 {i+1} 次: 骰子 {dice_list} → {rank_cn}'
                if rewards:
                    line += '，获得: ' + ', '.join(rewards)
                else:
                    line += '（+1次集礼盒）'
                self.logger.dice(line)

            rank_list.append(rank_cn + (f'[{",".join(rewards)}]' if rewards else ''))
            result['bobing_count'] = result.get('bobing_count', 0) + 1
            detail.append({
                'index': i + 1,
                'dice': dice_list,
                'rank': rank_cn,
                'rewards': rewards,
                'rest_count': rest_count,
            })

            if rest_count <= 0:
                break

        self.logger.success(f'博饼: {result.get("bobing_count", 0)}次 → ' + '，'.join(rank_list))

    # ---------- 集礼盒 ----------
    def collect_query_status(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026CollectService~queryStatus'
        return self._post(url)

    def collect(self) -> Optional[Dict]:
        if not self._consume('集礼盒'):
            return None
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026CollectService~collect'
        resp = self._post_full(url)
        if resp and resp.get('success'):
            return resp.get('obj')
        else:
            self.logger.warning(f'集礼盒失败: {self._err(resp)}')
            return None

    def query_weekly_collect_record(self) -> Optional[List]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026CollectService~queryWeeklyCollectRecord'
        return self._post(url)

    @staticmethod
    def _get_currency_balance(obj: Optional[Dict], currency: str) -> int:
        if not obj:
            return 0
        for acc in obj.get('currentAccountList', []):
            if acc.get('currency') == currency:
                return acc.get('balance', 0)
        return 0

    def do_collect(self, result: Dict) -> None:
        """集礼盒：消耗集礼盒次数，收集碎片"""
        status = self.collect_query_status()
        if not status:
            self.logger.warning('集礼盒: 状态获取失败')
            return

        collect_balance = self._get_currency_balance(status, 'COLLECT')
        completed = status.get('completedBoxCount', 0)
        if self.logger.verbose:
            fragments = {acc.get('currency'): acc.get('balance', 0)
                         for acc in status.get('currentAccountList', [])
                         if str(acc.get('currency', '')).startswith('FRAGMENT')}
            box_no = status.get('currentBoxNo', 0)
            box_status = status.get('currentBoxStatus', '')
            self.logger.info(f'集礼盒: 次数 {collect_balance}，已完成礼盒 {completed}，当前礼盒 第{box_no}个({box_status})')
            if fragments:
                self.logger.info('当前碎片: ' + ', '.join(f'{k}={v}' for k, v in fragments.items()))

        if collect_balance <= 0:
            self.logger.info('集礼盒: 无次数可用')
            return

        # 循环集礼盒，直到次数耗尽
        max_collect = 60
        cnt = 0
        gained: Dict[str, int] = {}
        while cnt < max_collect:
            time.sleep(1)
            c = self.collect()
            if not c:
                self.logger.warning('集礼盒返回空，停止')
                break

            received = c.get('receivedAccountList', [])
            if received:
                for item in received:
                    currency = item.get('currency', '未知')
                    amount = item.get('amount', 0)
                    gained[currency] = gained.get(currency, 0) + amount
                    if self.logger.verbose:
                        self.logger.medal(f'  获得碎片: {currency} x{amount}')
                result['collect_count'] = result.get('collect_count', 0) + 1

            collect_balance = self._get_currency_balance(c, 'COLLECT')
            if self.logger.verbose:
                if c.get('boxCompleted'):
                    self.logger.success(f'🎁 礼盒集齐！已完成 {c.get("completedBoxCount", 0)} 个礼盒')
                self.logger.info(f'  剩余集礼盒次数: {collect_balance}')

            if c.get('collectFinished'):
                if self.logger.verbose:
                    self.logger.success('所有礼盒已集齐，集礼盒结束')
                break
            if collect_balance <= 0:
                break
            cnt += 1

        gained_str = '，'.join(f'{k}x{v}' for k, v in gained.items()) if gained else '无'
        self.logger.success(f'集礼盒: {result.get("collect_count", 0)}次 → {gained_str}')

    # ---------- 抽奖 ----------
    def get_prize_pool(self) -> Optional[Dict]:
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026LotteryService~prizePool'
        return self._post(url)

    def prize_draw(self, lottery_type: str) -> Optional[Dict]:
        if not self._consume(f'抽奖({lottery_type})'):
            return None
        url = 'https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026LotteryService~prizeDraw'
        data = {"lotteryType": lottery_type}
        resp = self._post_full(url, data)
        if resp and resp.get('success'):
            return resp.get('obj')
        else:
            self.logger.warning(f'抽奖失败({lottery_type}): {self._err(resp)}')
            return None

    def do_lottery(self, result: Dict) -> None:
        """开礼盒单次抽奖 + 周四抽奖"""
        pool = self.get_prize_pool()
        if not pool:
            self.logger.warning('抽奖: 奖池获取失败')
            return

        # 单次抽奖奖池（集齐礼盒可抽）
        box_pool = pool.get('boxPool', {})
        remaining = box_pool.get('remainingDrawTimes', 0)
        if self.logger.verbose:
            box_gifts = box_pool.get('giftList', [])
            if box_gifts:
                self.logger.info('单抽奖品: ' + ', '.join(g.get('giftBagName', '?') for g in box_gifts))

        box_hits: List[str] = []
        if remaining > 0:
            for i in range(remaining):
                time.sleep(1)
                d = self.prize_draw("BOX")
                if not d:
                    self.logger.warning('单次抽奖失败，停止')
                    break
                name = d.get('giftBagName', '')
                worth = d.get('giftBagWorth', 0)
                pdto = d.get('productDTOList', [])
                if pdto:
                    p = pdto[0]
                    name = p.get('productName', p.get('couponName', name))
                box_hits.append(name or '奖励')
                result['box_draw_count'] = result.get('box_draw_count', 0) + 1
                result.setdefault('box_draw_results', []).append({'name': name, 'worth': worth})
                if self.logger.verbose:
                    self.logger.success(f'单次抽奖获得: {name or "奖励"} (价值{worth}元)')

        # 周四抽奖奖池
        weekly_pool = pool.get('weeklyPool', {})
        is_thursday = weekly_pool.get('isThursday', False)
        drawn = weekly_pool.get('drawn', False)

        weekly_hit = ''
        if is_thursday and not drawn:
            time.sleep(1)
            d = self.prize_draw("WEEKLY")
            if d:
                name = d.get('giftBagName', '')
                worth = d.get('giftBagWorth', 0)
                pdto = d.get('productDTOList', [])
                if pdto:
                    p = pdto[0]
                    name = p.get('productName', p.get('couponName', name))
                weekly_hit = name or '奖励'
                result['weekly_draw_count'] = result.get('weekly_draw_count', 0) + 1
                result.setdefault('weekly_draw_results', []).append({'name': name, 'worth': worth})
                if self.logger.verbose:
                    self.logger.success(f'周四抽奖获得: {weekly_hit} (价值{worth}元)')
            else:
                self.logger.warning('周四抽奖失败')

        if self.logger.verbose:
            self.logger.info(f'周四抽奖：isThursday={is_thursday}, drawn={drawn}')

        bits = [f'单抽{result.get("box_draw_count", 0)}次']
        if box_hits:
            bits.append('、'.join(box_hits))
        if is_thursday and not drawn and weekly_hit:
            bits.append('周四: ' + weekly_hit)
        elif is_thursday and drawn:
            bits.append('周四已参与')
        elif not is_thursday:
            bits.append('周四未开')
        self.logger.success('抽奖: ' + ' / '.join(bits))

    # ---------- 主流程 ----------
    def run(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'tasks_completed': 0,
            'bobing_count': 0,
            'collect_count': 0,
            'box_draw_count': 0,
            'weekly_draw_count': 0,
        }

        # 0. 进入活动首页（首次访问即带邀请参数，注册"被邀请访问"关系）
        inviter = self._pick_inviter()
        index_info = None
        if inviter and not self.dry_run:
            self.logger.detail(f'邀请访问: 携带邀请人 {inviter} (inviteType={INVITE_TYPE})')
            index_info = self.get_activity_index(invite_type=INVITE_TYPE, invite_user_id=inviter, no_login=True)
        if not index_info:
            index_info = self.get_activity_index()
        if index_info:
            ac_start = index_info.get("acStartTime", "")
            ac_end = index_info.get("acEndTime", "")
            send_num = index_info.get("sendNum", 0)
            pay_amount = index_info.get("payAmount", 0)
            self.logger.info(f'活动: {ac_start} ~ {ac_end}（寄件{send_num} / 支付{pay_amount}元）')

        # 1. 订阅状态（可选）+ 膨胀 + 挂件（进入页面触发）
        if ENABLE_SUBSCRIBE:
            subscribed = self.is_activity_subscribe()
            if subscribed:
                self.logger.info('订阅: 已订阅')
            else:
                self.logger.info('订阅: 未订阅，尝试订阅每日礼包...')
                self.do_subscribe()
        else:
            self.logger.detail('订阅: 跳过（SF_SUBSCRIBE=true 可开启）')
        self.get_dilate_change()
        widget = self.get_widget_status()
        if widget:
            dilate_widget = widget.get('dilateWidget', {})
            if dilate_widget:
                self.logger.detail(f'挂件领取状态: {dilate_widget.get("receiveStatus", "未知")}')
        dilate_status = self.get_dilate_status()
        if dilate_status:
            self.logger.detail(f'膨胀状态: dilateStatus={dilate_status.get("dilateStatus", "未知")}')

        # 2. 邀请列表（首次访问已注册邀请关系，这里查看结果）
        invite_list = self.get_invite_list()
        if invite_list:
            self.logger.success(f'邀请: {len(invite_list)} 位好友已访问')
            result['invited_friends'] = len(invite_list)
        else:
            self.logger.detail('暂无已邀请好友')

        # 3. 每日礼包
        self.do_daily_gift(result)

        # 4. 额外奖励卡片
        cards = self.query_extra_reward_cards()
        if cards:
            for card in cards:
                self.logger.detail(f'额外奖励 [{card.get("extraType", "")}]: 倒计时 {card.get("countdownHours", 0)}h')

        # 5. 双渠道任务（小程序与APP任务不同，分开做分开领）
        for idx, cfg in enumerate(CHANNELS):
            self._use_channel(cfg)
            self.logger.info(f'任务渠道: {cfg["name"]}')
            self.do_tasks(result)
            if idx == 0:
                # 一键集齐本周礼盒（账号级，跑一次即可）
                self.get_charge_task_reward()

        # 6. 博饼游戏（账号级每日一次，两个渠道的"玩博饼赢周边"任务共用）
        self.do_bobing(result)

        # 7. 博饼后补领游戏任务奖励（每渠道一次，把"玩博饼赢周边"+3次集礼盒拿全）
        if result.get('bobing_count', 0) > 0:
            for cfg in CHANNELS:
                self._use_channel(cfg)
                self._claim_task_rewards(result)

        # 8. 集礼盒
        self.do_collect(result)

        # 9. 抽奖
        self.do_lottery(result)

        # 10. 最终状态
        final_status = self.collect_query_status()
        if final_status:
            collect_balance = self._get_currency_balance(final_status, 'COLLECT')
            completed = final_status.get('completedBoxCount', 0)
            self.logger.info(f'最终: 集礼盒次数 {collect_balance}，已完成礼盒 {completed}')

        return result




# 模块级 logger（业务类内部引用）
logger = Logger(verbose=VERBOSE)


def verify_sf_cookie(cookie: str, proxies: Dict[str, str] | None) -> bool:
    """验证 Cookie 是否仍有效：拉一次活动页索引"""
    try:
        pm = ProxyManager("")
        http = SFHttpClient(pm)
        for item in cookie.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                http.session.cookies.set(k, v, domain="mcs-mimp-web.sf-express.com")
        data = http.request(
            "https://mcs-mimp-web.sf-express.com/mcs-mimp/commonPost/~memberNonactivity~midAutumn2026IndexService~index",
            {}, "POST",
        )
        return bool(data) and not data.get("errorMessage")
    except Exception:
        return False


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str]:
    """优先使用缓存 Cookie（活动索引验证），失效自动 code 刷新"""
    cache_cookie = get_cached_token(server)
    if cache_cookie:
        print("🔍 [缓存] 验证 Cookie")
        if verify_sf_cookie(cache_cookie, proxies):
            print("✅ [缓存] Cookie 有效")
            return cache_cookie, ""
        print("⚠️ [缓存] Cookie 已失效，重新登录")

    if FALLBACK_COOKIE:
        print("🔍 [兜底] 验证 SF_COOKIE")
        if verify_sf_cookie(FALLBACK_COOKIE, proxies):
            print("✅ [兜底] SF_COOKIE 有效")
            set_cached_token(server, FALLBACK_COOKIE)
            return FALLBACK_COOKIE, ""
        print("⚠️ [兜底] SF_COOKIE 无效，继续 code 登录")

    code = get_code(server)
    if not code:
        return None, "获取微信 code 失败"

    cookie = exchange_cookie_by_code(code, proxies, server)
    if not cookie:
        return None, "code 换 Cookie 失败"

    set_cached_token(server, cookie)
    return cookie, ""


class _Pm:
    """兼容 SFHttpClient 的代理管理器空实现（实际代理走品赞框架）"""

    def __init__(self):
        self.api_url = ""

    def get_proxy(self):
        return None


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "cookie": "-",
        "phone": "-",
        "tasks": "-",
        "bobing": "-",
        "collect": "-",
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

    cookie, err = login_with_cache(server, proxies)
    if not cookie:
        result["error"] = f"登录失败: {err}"
        return result

    result["cookie"] = mask(cookie)
    phone = ""
    if "_login_mobile_=" in cookie:
        phone = cookie.split("_login_mobile_=")[1].split(";")[0]
        result["phone"] = mask_mobile(phone)
    user_id = ""
    if "_login_user_id_=" in cookie:
        user_id = cookie.split("_login_user_id_=")[1].split(";")[0]

    print(f"👤 [账号] 手机: {mask_mobile(phone)}")

    try:
        # 用 顺丰中秋 原版客户端 + Cookie
        http = SFHttpClient(_Pm())
        for item in cookie.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                http.session.cookies.set(k, v, domain="mcs-mimp-web.sf-express.com")

        executor = MidAutumnExecutor(http, logger, user_id, dry_run=DRY_RUN, inviter_id="")
        activity_result = executor.run()

        result["tasks"] = str(activity_result.get("tasks_completed", 0))
        result["bobing"] = str(activity_result.get("bobing_count", 0))
        result["collect"] = str(activity_result.get("collect_count", 0))
        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🥮 顺丰中秋博饼 code 版任务结果

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
📱 手机：{res["phone"]}
🎯 任务：{res["tasks"]} 个
🎲 博饼：{res["bobing"]} 次
🎁 集礼盒：{res["collect"]} 次
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += "❌ 原因：" + str(res['error']) + "\n"

        content += "━━━━━━━━━━━━━━━━━━━━" + "\n"

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
                "cookie": "-",
                "phone": "-",
                "tasks": "-",
                "bobing": "-",
                "collect": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    for idx, res in enumerate(results, 1):
        if not res["success"] and res.get("error"):
            print(f"\n❌ [账号 {idx}] {res['server']} 失败原因：")
            for line in str(res["error"]).splitlines():
                print("   " + line)

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 顺丰中秋博饼任务执行完成                  ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🥮 顺丰中秋博饼任务完成", build_notify(results))


if __name__ == "__main__":
    main()
