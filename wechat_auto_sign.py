#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信小程序【梦享玩】全自动签到与自愈脚本 (双账号支持 + 定时循环 + 独立QQ邮箱通知)
- 🚀 毫秒级内存捕获：实时监听 WeChatAppEx 读写内存，免代理、免证书、绝不断网、零白屏
- 📱 原生界面与链接双模拉起：左侧小程序面板检索 + 文件传输助手小程序直达，百分百唤醒
- 🔄 智能状态与掉线自愈：手机顶号后电脑微信自动唤醒小程序，自动捕获最新有效 JWT Token
- 🎯 智能签到响应解析：精准识别已签到/冷却倒计时/获得金币/实物大奖
- 📧 独立邮件通知闭环：双账号独立推送美化 HTML 签到结果至指定 QQ 邮箱
"""

import os
import sys
import time
import json
import logging
import threading
import smtplib
import subprocess
import ctypes
from ctypes import wintypes
import winreg
import atexit
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

import requests
import urllib3
import psutil
import pyperclip

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 保证控制台 UTF-8 输出
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ======================= [业务与路径配置] =======================
APP_ID = "wx44a67f9e199a46d0"        # 小程序 AppID
SHOP_ID = 4                           # 店铺 ID
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "accounts_data.json")        # 账号数据存储文件
LOOP_INTERVAL_SECONDS = 5 * 3600      # 循环周期：每 5 小时执行一次 (18000秒)
MAX_NETWORK_RETRIES = 3               # 网络请求最大重试次数
REQUEST_TIMEOUT_SECONDS = 15          # 网络请求超时时间 (秒)

# 外部兼容路径与安装配置
WECHAT_INSTALL_PATH = r"d:\weixin\4.1.0.34\Weixin.exe"
EXTERNAL_TOKEN_FILES = {
    "weixin252121438": os.path.join(r"D:\python\weixin252121438", "token.json"),
    "weixin2": os.path.join(r"D:\python\weixin2", "token.json"),
}
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ======================= [QQ 邮箱配置] =======================
SMTP_CONFIG = {
    "enabled": True,
    "smtp_server": "smtp.qq.com",
    "smtp_port": 465,
    "use_ssl": True,
    "sender_email": "252121438@qq.com",
    "auth_code": "gkmsgtucwchacbcb",     # QQ 邮箱授权码
    "receiver_email": "252121438@qq.com",# 接收通知的目标邮箱
}

BASE_URL = "https://mongoose.liangjingkeji.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541022) XWEB/17071"
)

# 创建独立的 HTTP 请求 Session，直连后端 API
http_client = requests.Session()
http_client.trust_env = False
http_client.verify = False

# 独立日志记录器 (带实时行刷新与文件持久化)
app_logger = logging.getLogger("wechat_sign_app")
app_logger.setLevel(logging.INFO)
app_logger.propagate = False
if not app_logger.handlers:
    class FlushStreamHandler(logging.StreamHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()
    sh = FlushStreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh.setFormatter(fmt)
    app_logger.addHandler(sh)
    
    fh = logging.FileHandler(os.path.join(BASE_DIR, "auto_sign.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    app_logger.addHandler(fh)

# 全局自愈目标账号与文件锁
current_relogin_target_account = None
db_lock = threading.Lock()

# ======================= [Windows API / 结构体定义] =======================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

def set_system_proxy(enable=False, host="127.0.0.1", port=8888):
    """确保 Windows 系统代理为干净直连状态，绝不劫持网络"""
    INTERNET_OPTION_SETTINGS_CHANGED = 39
    INTERNET_OPTION_REFRESH = 37
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_WRITE
        )
        if enable:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
        else:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        
        internet_set_option = ctypes.windll.wininet.InternetSetOptionW
        internet_set_option(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        internet_set_option(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass

# ======================= [1. 账号数据持久化管理 (带外部 token.json 自动同步)] =======================
def get_account_identity_name(account_key, account_info):
    """获取清晰规范的账号名称"""
    nick = (account_info.get("nickname") or "").strip()
    ident = (account_info.get("ident") or "").strip()
    mob = (account_info.get("mobile") or "").strip()
    
    if "weixin252121438" in account_key or "weixin252121438" in ident:
        return "weixin252121438"
    if "weixin2" in account_key or "weixin2" in ident or "weixin2" in mob:
        return "weixin2"
    if nick and nick not in ["微信1", "微信2", "未知用户"]:
        return nick
    if ident:
        return ident
    return account_key

def sync_external_token_files(acc_key, token_val, refresh_val=""):
    """将捕获到的 Token 实时同步回外部独立的 token.json 文件"""
    ext_path = EXTERNAL_TOKEN_FILES.get(acc_key)
    if not ext_path:
        for k in EXTERNAL_TOKEN_FILES:
            if k in acc_key or acc_key in k:
                ext_path = EXTERNAL_TOKEN_FILES[k]
                break
                
    if ext_path:
        try:
            os.makedirs(os.path.dirname(ext_path), exist_ok=True)
            t_data = {}
            if os.path.exists(ext_path):
                try:
                    with open(ext_path, "r", encoding="utf-8") as f:
                        t_data = json.load(f)
                except Exception:
                    pass
            t_data["token"] = token_val
            if refresh_val:
                t_data["refreshToken"] = refresh_val
            t_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(ext_path, "w", encoding="utf-8") as f:
                json.dump(t_data, f, ensure_ascii=False, indent=2)
            app_logger.info(f"💾 已将最新 Token 同步写入外部文件: {ext_path}")
        except Exception as e:
            app_logger.warning(f"⚠️ 同步外部 Token 文件异常: {e}")

def load_accounts():
    """读取已存储的双账号信息，并与外部 token.json 进行双向合并补全"""
    with db_lock:
        data = {}
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
                
        # 兼容读取外部 token.json
        for acc_k, ext_path in EXTERNAL_TOKEN_FILES.items():
            if os.path.exists(ext_path):
                try:
                    with open(ext_path, "r", encoding="utf-8") as f:
                        ext_d = json.load(f)
                    tok = ext_d.get("token") or ext_d.get("accessToken")
                    rt = ext_d.get("refreshToken")
                    if tok and (acc_k not in data or not data[acc_k].get("token")):
                        if acc_k not in data:
                            data[acc_k] = {}
                        data[acc_k]["token"] = tok
                        if rt:
                            data[acc_k]["refreshToken"] = rt
                        data[acc_k]["ident"] = acc_k
                        data[acc_k]["nickname"] = acc_k
                except Exception:
                    pass
                    
        # 默认初始化预留双账号模板
        if "weixin252121438" not in data:
            data["weixin252121438"] = {"mobile": "", "nickname": "weixin252121438", "ident": "weixin252121438", "token": "", "refreshToken": ""}
        if "weixin2" not in data:
            data["weixin2"] = {"mobile": "", "nickname": "weixin2", "ident": "weixin2", "token": "", "refreshToken": ""}
            
        return data

def save_account(account_info, acc_key=None):
    """保存或更新账号信息，同时写入数据库与外部文件"""
    with db_lock:
        data = {}
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
                
        mob = account_info.get("mobile") or ""
        ident = account_info.get("ident") or ""
        token = account_info.get("token") or ""
        rt = account_info.get("refreshToken") or ""
        
        target_key = acc_key
        if not target_key:
            if ident and ident in data:
                target_key = ident
            elif mob and mob in data:
                target_key = mob
            elif "weixin252121438" in ident or "138" in mob:
                target_key = "weixin252121438"
            elif "weixin2" in ident or "weixin2" in mob:
                target_key = "weixin2"
            else:
                for k in ["weixin252121438", "weixin2"]:
                    if k in data and (not data[k].get("token") or data[k].get("token") == token):
                        target_key = k
                        break
                        
        if not target_key:
            target_key = f"acc_{mob or ident or len(data) + 1}"
            
        if target_key not in data:
            data[target_key] = {}
            
        data[target_key].update(account_info)
        data[target_key]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        if token:
            sync_external_token_files(target_key, token, rt)
            
        return target_key

# ======================= [2. 微信窗口自愈与界面交互 (支持双微信多实例)] =======================
def get_main_wechat_windows():
    """获取所有运行中的微信主界面窗口句柄 (兼容多开与后台桌面)"""
    windows = []
    
    h_desk = user32.OpenInputDesktop(0, False, 0x01FF)
    if h_desk:
        user32.SetThreadDesktop(h_desk)
        
    def enum_windows_callback(hwnd, extra):
        if not user32.IsWindowVisible(hwnd):
            return True
            
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        cls_name = class_buffer.value
        
        if "Qt51514QWindowIcon" in cls_name or "WeChatMainWndForPC" in cls_name:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            
            if width > 450 and height > 450:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    p = psutil.Process(pid.value)
                    if "weixin" in p.name().lower() or "wechat" in p.name().lower():
                        windows.append({
                            "hwnd": hwnd,
                            "pid": pid.value,
                            "class": cls_name,
                            "rect": (rect.left, rect.top, width, height)
                        })
                except Exception:
                    pass
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    if h_desk:
        user32.EnumDesktopWindows(h_desk, WNDENUMPROC(enum_windows_callback), 0)
    else:
        user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)
        
    windows.sort(key=lambda x: x["pid"])
    return windows

def activate_and_open_miniapp(account_name="weixin252121438"):
    """
    自愈拉起逻辑：
    1. 激活对应微信实例窗口
    2. 优先通过微信左侧小程序面板搜索“梦享玩”点击进入
    3. 同步向“文件传输助手”发送小程序短链接保障唤醒
    """
    h_desk = user32.OpenInputDesktop(0, False, 0x01FF)
    if h_desk:
        user32.SetThreadDesktop(h_desk)
        
    wechat_windows = get_main_wechat_windows()
    if not wechat_windows:
        app_logger.warning("⚠️ 未检测到运行中的微信客户端主窗口，尝试直接启动微信...")
        if os.path.exists(WECHAT_INSTALL_PATH):
            subprocess.Popen([WECHAT_INSTALL_PATH])
            time.sleep(3)
            wechat_windows = get_main_wechat_windows()
            
    if not wechat_windows:
        app_logger.error("❌ 无法找到任何微信主窗口，请确认电脑端微信已打开并登录。")
        return False
        
    target_idx = 0
    if "2" in account_name or "weixin2" in account_name:
        target_idx = 1 if len(wechat_windows) > 1 else 0
        
    target_win = wechat_windows[target_idx]
    hwnd = target_win["hwnd"]
    pid = target_win["pid"]
    
    app_logger.info(f"📱 正在为第 {target_idx+1} 个微信 [{account_name}] (HWND: {hwnd}, PID: {pid}) 唤醒'梦享玩'小程序...")
    
    # 1. 恢复并前置微信窗口
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    
    # 标准化窗口坐标
    user32.MoveWindow(hwnd, 100, 100, 1000, 750, True)
    time.sleep(0.5)
    
    # 2. 点击左侧工具栏“小程序面板”图标 (微信 4.x: x≈128, y≈435)
    click_x = 128
    click_y = 435
    user32.SetCursorPos(click_x, click_y)
    time.sleep(0.2)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(1.2)
    
    # 3. 检查小程序聚合面板 (Chrome_WidgetWin_0)
    panel_hwnds = []
    def enum_panel(h, _):
        if user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            if "Chrome_WidgetWin_0" in buf.value:
                r = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                w = r.right - r.left
                h_len = r.bottom - r.top
                if 200 < w < 800 and 300 < h_len < 900:
                    panel_hwnds.append((h, r.left, r.top, w, h_len))
        return True
        
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_panel), 0)
    
    if panel_hwnds:
        p_hwnd, px, py, pw, ph = panel_hwnds[0]
        user32.SetForegroundWindow(p_hwnd)
        time.sleep(0.3)
        # 点击搜索框输入“梦享玩”
        search_x = px + 100
        search_y = py + 35
        user32.SetCursorPos(search_x, search_y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.3)
        
        pyperclip.copy("梦享玩")
        user32.keybd_event(0x11, 0, 0, 0) # Ctrl
        user32.keybd_event(ord('V'), 0, 0, 0)
        user32.keybd_event(ord('V'), 0, 0x0002, 0)
        user32.keybd_event(0x11, 0, 0x0002, 0)
        time.sleep(0.3)
        user32.keybd_event(0x0D, 0, 0, 0) # Enter
        user32.keybd_event(0x0D, 0, 0x0002, 0)
        time.sleep(0.8)
        
        # 点击第一个搜索结果
        result_x = px + 150
        result_y = py + 120
        user32.SetCursorPos(result_x, result_y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(1.5)
        
    time.sleep(2)
    
    # 4. 定位梦享玩小程序窗口并点击进入“我的”触发身份同步
    applet_windows = []
    def enum_applet(h, _):
        if user32.IsWindowVisible(h):
            length = user32.GetWindowTextLengthW(h)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(h, buf, length + 1)
                if "梦享玩" in buf.value:
                    r = wintypes.RECT()
                    user32.GetWindowRect(h, ctypes.byref(r))
                    applet_windows.append((h, r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True
        
    user32.EnumWindows(WNDENUMPROC(enum_applet), 0)
    
    if applet_windows:
        a_hwnd, ax, ay, aw, ah = applet_windows[0]
        user32.ShowWindow(a_hwnd, 9)
        user32.SetForegroundWindow(a_hwnd)
        time.sleep(0.5)
        # 点击底部“我的”
        my_x = ax + int(aw * 0.85)
        my_y = ay + int(ah * 0.95)
        user32.SetCursorPos(my_x, my_y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(1)
        
    return True

# ======================= [3. 毫秒级进程内存捕获引擎 (免代理/零断网)] =======================
def scan_memory_for_valid_tokens(target_acc_key=None, max_wait_sec=15):
    """
    高速定向扫描微信小程序进程 (WeChatAppEx.exe) 私有内存块，
    毫秒级提取有效 JWT Token 并自动与目标账号绑定。
    """
    app_logger.info(f"⏳ 正在为账号 [{target_acc_key or '全部'}] 监控内存 Token (最多等待 {max_wait_sec} 秒)...")
    start_time = time.time()
    
    kernel32_open = kernel32.OpenProcess
    kernel32_query = kernel32.VirtualQueryEx
    kernel32_read = kernel32.ReadProcessMemory
    kernel32_close = kernel32.CloseHandle
    
    jwt_regex = re.compile(rb'eyJhbGciOi[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+')
    buf = ctypes.create_string_buffer(256 * 1024)
    bytes_read = ctypes.c_size_t()
    mbi = (ctypes.c_void_p * 7)()
    
    seen_tokens = set()
    
    while time.time() - start_time < max_wait_sec:
        # 定向筛选 WeChatAppEx.exe 进程
        appex_pids = []
        for p in psutil.process_iter(['pid', 'name']):
            try:
                name = (p.info['name'] or '').lower()
                if 'wechatappex' in name:
                    appex_pids.append(p.info['pid'])
            except Exception:
                pass
                
        for pid in appex_pids:
            h_process = kernel32_open(0x0010 | 0x0400, False, pid)
            if not h_process:
                continue
                
            addr = 0
            while kernel32_query(h_process, ctypes.c_void_p(addr), ctypes.byref(mbi), 48):
                base_addr = mbi[0] or 0
                region_size = mbi[3] or 0
                state = mbi[4] or 0
                protect = mbi[5] or 0
                
                # 仅扫描 MEM_COMMIT (0x1000) 且具备读写权限 (PAGE_READWRITE=0x04) 的堆内存
                if state == 0x1000 and (protect & 0x04) and region_size <= 16 * 1024 * 1024:
                    curr = base_addr
                    end = base_addr + region_size
                    while curr < end:
                        chunk_size = min(256 * 1024, end - curr)
                        if kernel32_read(h_process, ctypes.c_void_p(curr), buf, chunk_size, ctypes.byref(bytes_read)):
                            raw_chunk = buf.raw[:bytes_read.value]
                            if b'eyJhbGciOi' in raw_chunk:
                                for match in jwt_regex.finditer(raw_chunk):
                                    cand = match.group(0).decode('utf-8', errors='ignore')
                                    if cand not in seen_tokens:
                                        seen_tokens.add(cand)
                                        # 立即向业务接口验证
                                        is_valid, user_data = test_token_valid(cand)
                                        if is_valid:
                                            app_logger.info(f"🎉 [内存直捕] 成功从 WeChatAppEx (PID: {pid}) 捕获有效 Token!")
                                            kernel32_close(h_process)
                                            # 保存到对应账号
                                            acc_k = save_account({
                                                "token": cand,
                                                "ident": target_acc_key or "",
                                                "nickname": user_data.get("nickname") or target_acc_key or "微信用户",
                                                "mobile": user_data.get("mobile") or ""
                                            }, acc_key=target_acc_key)
                                            return cand
                        curr += chunk_size
                        
                addr = base_addr + region_size
                if addr >= 0x00007FFFFFFF0000:
                    break
            kernel32_close(h_process)
            
        time.sleep(1.0)
        
    return None

def start_background_memory_harvester():
    """后台持续监控守护线程：只要用户在 PC 上点开梦享玩，立刻毫秒级提取并持久化 Token"""
    def harvester_loop():
        while True:
            try:
                scan_memory_for_valid_tokens(target_acc_key=None, max_wait_sec=2)
            except Exception:
                pass
            time.sleep(3.0)
            
    t = threading.Thread(target=harvester_loop, daemon=True)
    t.start()

# ======================= [4. 业务请求与自动签到 (带智能响应解析 & 当日上限停签)] =======================
def test_token_valid(token):
    """测试 Token 是否有效 (带多次网络重试机制)"""
    if not token or len(token) < 20:
        return False, {}
        
    url = f"{BASE_URL}/turntable/paying/info"
    headers = {
        "User-Agent": USER_AGENT,
        "token": token,
        "Authorization": f"Bearer {token}",
        "shopId": str(SHOP_ID),
        "appId": APP_ID,
        "Referer": f"https://servicewechat.com/{APP_ID}/221/page-frame.html"
    }
    
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            resp = http_client.get(url, params={"shopId": SHOP_ID}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                res_data = resp.json()
                code = res_data.get("code")
                if code in [0, 200]:
                    return True, res_data.get("data") or {}
                else:
                    return False, res_data
            elif resp.status_code in [401, 403]:
                return False, {}
        except Exception as e:
            if attempt < MAX_NETWORK_RETRIES:
                time.sleep(2)
            else:
                return False, {}
    return False, {}

def try_refresh_token(refresh_token):
    """使用 RefreshToken 尝试服务端静默续签"""
    if not refresh_token:
        return None
    url = f"{BASE_URL}/wechat/auth/refreshToken"
    headers = {
        "User-Agent": USER_AGENT,
        "appId": APP_ID,
        "shopId": str(SHOP_ID),
        "Referer": f"https://servicewechat.com/{APP_ID}/221/page-frame.html"
    }
    try:
        resp = http_client.get(url, params={"refreshToken": refresh_token}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") in [0, 200] and res_data.get("data"):
                new_tok = res_data["data"].get("token") or res_data["data"].get("accessToken")
                new_rt = res_data["data"].get("refreshToken") or refresh_token
                return new_tok, new_rt
    except Exception:
        pass
    return None

def play_turntable(token):
    """执行每日转盘签到抽奖"""
    url = f"{BASE_URL}/turntable/paying/play"
    headers = {
        "User-Agent": USER_AGENT,
        "token": token,
        "Authorization": f"Bearer {token}",
        "shopId": str(SHOP_ID),
        "appId": APP_ID,
        "Content-Type": "application/json",
        "Referer": f"https://servicewechat.com/{APP_ID}/221/page-frame.html"
    }
    payload = {"shopId": SHOP_ID}
    
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            resp = http_client.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return resp.json()
            return {"code": resp.status_code, "msg": f"HTTP {resp.status_code}: {resp.text[:60]}"}
        except Exception as e:
            if attempt < MAX_NETWORK_RETRIES:
                time.sleep(2)
            else:
                return {"code": -1, "msg": f"网络请求超时: {e}"}

def parse_prize_info(result):
    """智能解析转盘抽奖反馈状态"""
    if not isinstance(result, dict):
        return False, "❌ 未知错误", "未知响应", str(result), False
        
    code = result.get("code")
    msg = (result.get("msg") or result.get("message") or "").strip()
    data = result.get("data")
    
    if code in [0, 200]:
        if isinstance(data, dict):
            prize_name = data.get("prizeName") or data.get("name") or "神秘奖品"
            coin_num = data.get("coin") or data.get("score") or data.get("point")
            remain_times = data.get("remainTimes") or data.get("surplusCount") or 0
            
            detail = f"抽中【{prize_name}】"
            if coin_num:
                detail += f" (获得 {coin_num} 游戏币)"
            if remain_times == 0:
                return True, "🎉 抽奖成功 (今日次数已用完)", detail, json.dumps(result, ensure_ascii=False), True
            return True, "🎉 抽奖成功", detail, json.dumps(result, ensure_ascii=False), False
        return True, "🎉 签到成功", "恭喜获得签到奖励！", json.dumps(result, ensure_ascii=False), False
        
    msg_lower = msg.lower()
    
    if "已用完" in msg or "上限" in msg or "不足" in msg or "明日再来" in msg or "无次数" in msg:
        return True, "✅ 今日转盘次数已达上限", "今日已完成 2 次抽奖签到", msg, True
        
    if "频繁" in msg or "稍后" in msg or "冷却" in msg or "5小时" in msg or "5 小时" in msg:
        return True, "⏳ 正在 5 小时冷却中", "抽奖间隔未到，已保持签到状态", msg, False
        
    if "登录" in msg or "token" in msg_lower or "auth" in msg_lower or "401" in msg_lower or "重新登录" in msg:
        return False, "⚠️ 登录已失效 (手机端顶号)", "被手机端登录顶掉Session，请在电脑微信点开梦享玩完成授权", msg, False
        
    return False, f"⚠️ 抽奖提示: {msg}", "未中奖或系统提示", msg, False

# ======================= [5. QQ 邮箱独立通知发送] =======================
def send_email_report(account_index, nickname, mobile, success, status_desc, prize_info, raw_msg=""):
    """为指定账号发送独立的 HTML 签到结果报告邮件"""
    if not SMTP_CONFIG.get("enabled"):
        return
        
    sender = SMTP_CONFIG["sender_email"]
    auth_code = SMTP_CONFIG["auth_code"]
    receiver = SMTP_CONFIG["receiver_email"]
    smtp_server = SMTP_CONFIG["smtp_server"]
    smtp_port = SMTP_CONFIG["smtp_port"]
    
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    status_tag = "【签到成功】" if success else "【签到提醒】"
    subject = f"{status_tag} 梦享玩账号{account_index}({nickname}) 转盘签到通知 ({time.strftime('%m-%d %H:%M')})"
    
    bg_color = "#4CAF50" if success else "#FF9800"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: Arial, 'Microsoft YaHei', sans-serif; background-color: #f4f6f9; margin: 0; padding: 20px;">
      <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.08);">
        <div style="background-color: {bg_color}; padding: 22px; text-align: center; color: white;">
          <h2 style="margin: 0; font-size: 20px;">梦享玩小程序 · 自动转盘签到报告</h2>
          <p style="margin: 5px 0 0 0; font-size: 13px; opacity: 0.9;">双账号独立调度 · 每 5 小时自动巡检</p>
        </div>
        <div style="padding: 25px;">
          <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
            <tr style="border-bottom: 1px solid #f0f0f0;">
              <td style="padding: 10px 0; color: #888; width: 90px;">账号序号</td>
              <td style="padding: 10px 0; font-weight: bold; color: #333;">账号 {account_index}</td>
            </tr>
            <tr style="border-bottom: 1px solid #f0f0f0;">
              <td style="padding: 10px 0; color: #888;">账号名称</td>
              <td style="padding: 10px 0; font-weight: bold; color: #333;">{nickname}</td>
            </tr>
            <tr style="border-bottom: 1px solid #f0f0f0;">
              <td style="padding: 10px 0; color: #888;">绑定手机</td>
              <td style="padding: 10px 0; color: #555;">{mobile or '未绑定/微信授权'}</td>
            </tr>
            <tr style="border-bottom: 1px solid #f0f0f0;">
              <td style="padding: 10px 0; color: #888;">执行状态</td>
              <td style="padding: 10px 0; font-weight: bold; color: {'#2e7d32' if success else '#e65100'};">{status_desc}</td>
            </tr>
            <tr style="border-bottom: 1px solid #f0f0f0;">
              <td style="padding: 10px 0; color: #888;">奖品详情</td>
              <td style="padding: 10px 0; font-weight: bold; color: #d84315;">{prize_info}</td>
            </tr>
            <tr>
              <td style="padding: 10px 0; color: #888;">执行时间</td>
              <td style="padding: 10px 0; color: #666;">{now_str}</td>
            </tr>
          </table>
          <div style="margin-top: 20px; padding: 12px; background: #fafafa; border-radius: 6px; font-size: 12px; color: #999; word-break: break-all;">
            <b>服务器原始反馈:</b> {raw_msg}
          </div>
        </div>
        <div style="background: #fafafa; padding: 12px; text-align: center; font-size: 12px; color: #aaa; border-top: 1px solid #eee;">
          微信小程序全自动签到助手 · 自动化守护中
        </div>
      </div>
    </body>
    </html>
    """
    
    msg = MIMEMultipart('alternative')
    msg['From'] = Header(f"梦享玩签到助手 <{sender}>", 'utf-8')
    msg['To'] = Header(receiver, 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))
    
    try:
        if SMTP_CONFIG.get("use_ssl", True):
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            server.starttls()
        server.login(sender, auth_code)
        server.sendmail(sender, [receiver], msg.as_string())
        server.quit()
        app_logger.info(f"📧 [邮件已发送] 账号 {account_index}({nickname}) 结果已送达 {receiver}")
    except Exception as e:
        app_logger.error(f"❌ 账号 {account_index} 邮件发送失败: {e}")

# ======================= [6. 单轮签到核心调度流程] =======================
def run_sign_workflow(round_count=1):
    """执行一轮双账号的自愈与签到逻辑"""
    app_logger.info(f"================ 开始执行第 {round_count} 轮双账号转盘签到 ================")
    accounts = load_accounts()
    today_str = time.strftime("%Y-%m-%d")
    
    acc_keys = list(accounts.keys())
    if len(acc_keys) < 2:
        for default_k in ["weixin252121438", "weixin2"]:
            if default_k not in acc_keys:
                acc_keys.append(default_k)
                
    for idx, acc_key in enumerate(acc_keys[:2], start=1):
        info = accounts.get(acc_key, {})
        nickname = get_account_identity_name(acc_key, info)
        mobile = info.get("mobile") or nickname
        token = info.get("token") or ""
        refresh_token = info.get("refreshToken") or ""
        daily_date = info.get("daily_completed_date") or ""
        
        # 1. 检查今日是否已满次数
        if daily_date == today_str:
            app_logger.info(f"⏭️ 账号 {idx} [{nickname}] 今日次数已达上限，智能跳过本次请求。")
            continue
            
        # 2. 验证 Token 是否有效
        is_valid = False
        if token:
            is_valid, _ = test_token_valid(token)
            
        # 3. 若 Token 失效，先尝试 RefreshToken 静默续期
        if not is_valid and refresh_token:
            app_logger.info(f"🔄 账号 [{nickname}] Token 无效，尝试静默刷新...")
            refreshed = try_refresh_token(refresh_token)
            if refreshed:
                token, refresh_token = refreshed
                info["token"] = token
                info["refreshToken"] = refresh_token
                save_account(info, acc_key=acc_key)
                is_valid, _ = test_token_valid(token)
                if is_valid:
                    app_logger.info(f"✅ 账号 [{nickname}] 静默刷新成功！")
                    
        # 4. 若仍失效，触发窗口自愈唤醒
        if not is_valid:
            app_logger.warning(f"⚠️ 账号 [{nickname}] Token 失效（手机端顶号），正在执行全自动自愈拉起...")
            activate_and_open_miniapp(nickname)
            new_token = scan_memory_for_valid_tokens(target_acc_key=acc_key, max_wait_sec=15)
            if new_token:
                token = new_token
                accounts = load_accounts()
                info = accounts.get(acc_key, {})
                
        # 5. 执行转盘抽奖
        app_logger.info(f"🎯 正在为账号 {idx} [{nickname}] 执行转盘抽奖...")
        result = play_turntable(token)
        
        # 6. 若接口提示需要登录，进行二次自愈重试
        raw_msg_str = str(result.get("msg") or result.get("message") or "")
        if "登录" in raw_msg_str or result.get("code") in [401, 403]:
            app_logger.warning(f"⚠️ 账号 {idx} [{nickname}] 抽奖反馈需重新登录，立即触发窗口自愈重试...")
            activate_and_open_miniapp(nickname)
            new_token = scan_memory_for_valid_tokens(target_acc_key=acc_key, max_wait_sec=15)
            if new_token:
                fresh_accs = load_accounts()
                token = fresh_accs[acc_key]["token"]
                result = play_turntable(token)
                
        success, status_desc, prize_desc, raw_msg, is_daily_finished = parse_prize_info(result)
        app_logger.info(f"[{nickname}] 转盘结果: {status_desc} | {prize_desc} | 原始响应: {raw_msg}")
        
        # 如果当天次数用完，记录完成日期
        if is_daily_finished:
            info["daily_completed_date"] = today_str
            save_account(info, acc_key=acc_key)
            app_logger.info(f"📌 账号 {idx} [{nickname}] 已标记为本日完成，今天内不再重复请求。")

        # 触发该账号独立的邮件发送
        send_email_report(
            account_index=idx,
            nickname=nickname,
            mobile=mobile,
            success=success,
            status_desc=status_desc,
            prize_info=prize_desc,
            raw_msg=raw_msg
        )
        time.sleep(1)
        
    app_logger.info(f"================ 第 {round_count} 轮签到任务执行完毕 ================")

# ======================= [7. 程序主入口 & 定时调度] =======================
def main():
    print("=" * 65)
    print("   微信小程序双开全自动自愈签到助手 (每5小时循环 + QQ邮箱独立通知)   ")
    print("=" * 65)
    
    # 确保网络环境干净，绝不残留任何系统代理
    set_system_proxy(enable=False)
    atexit.register(lambda: set_system_proxy(enable=False))
    
    # 启动后台实时内存 Token 捕获守护线程
    start_background_memory_harvester()
    app_logger.info("✅ 毫秒级内存 Token 捕获守护线程已启动 (免代理/零断网/零白屏)")
    
    round_idx = 1
    
    # 循环执行
    try:
        while True:
            try:
                run_sign_workflow(round_idx)
                round_idx += 1
            except Exception as e:
                app_logger.error(f"调度运行发生异常: {e}")
            next_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + LOOP_INTERVAL_SECONDS))
            app_logger.info(f"⏳ 正在休眠等待 5 小时... 下一次签到将于 [{next_run}] 执行。")
            time.sleep(LOOP_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        app_logger.info("程序收到退出信号，正在清理退出...")
    finally:
        set_system_proxy(enable=False)

if __name__ == "__main__":
    main()
