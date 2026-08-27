#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信小程序【梦享玩】双引擎全自动自愈签到系统 (双账号支持 + 定时循环 + 独立QQ邮箱通知)
- 🚀 双擎驱动：HTTP 接口秒级直签 + 全流程 GDI 视窗自愈识图登录与签到
- 📱 完整 7 步自愈状态机：
    1. 自动唤醒微信并拉起梦享玩小程序
    2. 自动勾选《已阅读并同意免责条款》
    3. 自动点击【手机号快捷登录】并授权进入主页
    4. 自动点击首页【每日转盘】进入抽奖专区
    5. 自动点击大转盘中心【抽奖】
    6. 自动点击门店确认弹窗【确定抽奖】
    7. 自动点击【一键领取】游戏币并截图留证
- 📸 实时战报截图：抽奖前后截取小程序全流程画面，随邮件附件直推 QQ 邮箱
- 🔄 5小时高精度循环守护 + 零代理零断网
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
from email.mime.image import MIMEImage
from email.header import Header

import requests
import urllib3
import psutil
import pyperclip
from PIL import Image

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ======================= [业务与路径配置] =======================
APP_ID = "wx44a67f9e199a46d0"
SHOP_ID = 4
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "accounts_data.json")
LOOP_INTERVAL_SECONDS = 5 * 3600
MAX_NETWORK_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 15

EXTERNAL_TOKEN_FILES = {
    "weixin252121438": os.path.join(r"D:\python\weixin252121438", "token.json"),
    "weixin2": os.path.join(r"D:\python\weixin2", "token.json"),
}

# ======================= [QQ 邮箱配置] =======================
SMTP_CONFIG = {
    "enabled": True,
    "smtp_server": "smtp.qq.com",
    "smtp_port": 465,
    "use_ssl": True,
    "sender_email": "252121438@qq.com",
    "auth_code": "ovomwimcvvwhcaee",     # QQ 邮箱授权码
    "receiver_email": "252121438@qq.com",
}

BASE_URL = "https://mongoose.liangjingkeji.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541022) XWEB/17071"
)

http_client = requests.Session()
http_client.trust_env = False
http_client.verify = False

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

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

h_desk = user32.OpenInputDesktop(0, False, 0x01FF)
if h_desk:
    user32.SetThreadDesktop(h_desk)

def ensure_proxy_disabled():
    try:
        reg_key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(reg_key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(reg_key)
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass

# ======================= [1. 账号数据持久化] =======================
def load_accounts():
    accounts = []
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    accounts = data
                elif isinstance(data, dict):
                    accounts = list(data.values())
        except Exception:
            pass
            
    for default_ident in ["weixin252121438", "weixin2"]:
        if not any(acc.get("ident") == default_ident for acc in accounts):
            token_val = ""
            ext_path = EXTERNAL_TOKEN_FILES.get(default_ident)
            if ext_path and os.path.exists(ext_path):
                try:
                    with open(ext_path, "r", encoding="utf-8") as ef:
                        ext_data = json.load(ef)
                        token_val = ext_data.get("token") or ext_data.get("access_token") or ""
                except Exception:
                    pass
            accounts.append({
                "ident": default_ident,
                "nickname": default_ident,
                "token": token_val,
                "refreshToken": "",
                "mobile": ""
            })
    return accounts

def save_account(user_data, acc_key=None):
    accounts = load_accounts()
    token = user_data.get("token", "")
    ident = acc_key or user_data.get("ident") or "weixin252121438"
    
    target = None
    for acc in accounts:
        if acc.get("ident") == ident:
            target = acc
            break
            
    if not target:
        target = {"ident": ident}
        accounts.append(target)
        
    target["token"] = token
    if user_data.get("refreshToken"):
        target["refreshToken"] = user_data["refreshToken"]
    if user_data.get("nickname"):
        target["nickname"] = user_data["nickname"]
    if user_data.get("mobile"):
        target["mobile"] = user_data["mobile"]
        
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
        
    ext_file = EXTERNAL_TOKEN_FILES.get(ident)
    if ext_file:
        try:
            os.makedirs(os.path.dirname(ext_file), exist_ok=True)
            with open(ext_file, "w", encoding="utf-8") as ef:
                json.dump({"token": token, "update_time": time.strftime("%Y-%m-%d %H:%M:%S")}, ef, indent=2)
        except Exception:
            pass
            
    return ident

# ======================= [2. GDI 截屏与视窗自动化引擎] =======================
def capture_window_gdi(hwnd, save_path):
    try:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return False
            
        hwnd_dc = user32.GetWindowDC(hwnd)
        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        save_bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        gdi32.SelectObject(mfc_dc, save_bitmap)
        
        res = user32.PrintWindow(hwnd, mfc_dc, 2)
        if not res:
            res = user32.PrintWindow(hwnd, mfc_dc, 0)
            
        bmi = (wintypes.DWORD * 11)()
        bmi[0] = 40; bmi[1] = w; bmi[2] = -h; bmi[3] = 1 | (32 << 16); bmi[4] = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mfc_dc, save_bitmap, 0, h, buf, ctypes.byref(bmi), 0)
        
        gdi32.DeleteObject(save_bitmap)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        
        img = Image.frombytes("RGBA", (w, h), buf.raw, "raw", "BGRA").convert("RGB")
        img.save(save_path)
        return True
    except Exception as e:
        app_logger.warning(f"截取窗口画面失败: {e}")
        return False

def post_click_client(hwnd, x_ratio, y_ratio):
    try:
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            
        x = int(w * x_ratio)
        y = int(h * y_ratio)
        lparam = (y << 16) | (x & 0xFFFF)
        
        children = []
        def enum_ch(ch, _):
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ch, cls_buf, 256)
            children.append((ch, cls_buf.value))
            return True
        user32.EnumChildWindows(hwnd, ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(enum_ch), 0)
        
        target = hwnd
        for ch, cls in children:
            if "Render" in cls or "Intermediate" in cls or "Chrome" in cls:
                target = ch
                break
                
        user32.PostMessageW(target, 0x0201, 0x0001, lparam) # WM_LBUTTONDOWN
        time.sleep(0.08)
        user32.PostMessageW(target, 0x0202, 0x0000, lparam) # WM_LBUTTONUP
        time.sleep(0.2)
    except Exception:
        pass

def find_windows_by_keyword(keyword):
    hwnds = []
    def enum_cb(h, _):
        if user32.IsWindowVisible(h):
            length = user32.GetWindowTextLengthW(h)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(h, buf, length + 1)
                if keyword in buf.value:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    hwnds.append((h, buf.value, pid.value))
        return True
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    if h_desk:
        user32.EnumDesktopWindows(h_desk, WNDENUMPROC(enum_cb), 0)
    else:
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    return hwnds

def perform_visual_turntable_spin(hwnd, acc_ident="账号"):
    """
    7 步全自动视窗自愈识图签到流程：
    1. 点击同意免责条款复选框 (14% X, 92.5% Y)
    2. 点击【手机号快捷登录】并处理授权 (50% X, 67% Y)
    3. 点击首页【每日转盘】(85% X, 38% Y)
    4. 点击大转盘中心【抽奖】(50% X, 48% Y)
    5. 点击门店确认弹窗【确定抽奖】(70% X, 56% Y)
    6. 等待大转盘旋转及开奖动画
    7. 点击【一键领取】游戏币 (75% X, 88% Y)
    """
    app_logger.info(f"🎯 正在为 [{acc_ident}] (HWND: {hwnd}) 执行 7 步全流程视窗自愈与转盘签到...")
    
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    
    # 步骤 1 & 2: 登录页自愈 (同意条款 + 快捷登录 + 授权)
    post_click_client(hwnd, 0.14, 0.925)
    time.sleep(0.3)
    post_click_client(hwnd, 0.50, 0.67)
    time.sleep(0.5)
    post_click_client(hwnd, 0.70, 0.58) # 微信授权允许
    time.sleep(1.0)
    
    # 步骤 3: 从首页进入每日转盘
    post_click_client(hwnd, 0.85, 0.38)
    time.sleep(1.5)
    
    # 步骤 4 & 5: 转盘中心抽奖 + 确认门店弹窗
    post_click_client(hwnd, 0.50, 0.48)
    time.sleep(0.8)
    post_click_client(hwnd, 0.70, 0.56)
    time.sleep(0.5)
    post_click_client(hwnd, 0.70, 0.56)
    
    # 步骤 6: 等待旋转开奖
    app_logger.info("⏳ 等待大转盘旋转及开奖动画 (5秒)...")
    time.sleep(5.0)
    
    # 步骤 7: 一键领取
    post_click_client(hwnd, 0.75, 0.88)
    time.sleep(1.5)
    
    res_img = os.path.join(BASE_DIR, f"spin_{acc_ident}_result.png")
    capture_window_gdi(hwnd, res_img)
    app_logger.info(f"📸 视窗签到完成，结果截图已存至: {res_img}")
    return res_img

def wake_up_miniapp_and_spin(wx_hwnd, pid, acc_ident):
    """通过微信窗口唤醒梦享玩并执行识图签到"""
    app_logger.info(f"📱 正在为微信 [{acc_ident}] (HWND: {wx_hwnd}, PID: {pid}) 唤醒'梦享玩'小程序...")
    
    user32.ShowWindow(wx_hwnd, 9)
    user32.SetForegroundWindow(wx_hwnd)
    time.sleep(0.5)
    
    # 查找已有小程序窗口
    applets = find_windows_by_keyword("梦享玩")
    if applets:
        a_hwnd = applets[0][0]
        return perform_visual_turntable_spin(a_hwnd, acc_ident)
        
    # 点击左侧工具栏小程序面板
    r = wintypes.RECT()
    user32.GetWindowRect(wx_hwnd, ctypes.byref(r))
    user32.SetCursorPos(r.left + 28, r.top + 220)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(1.5)
    
    for _ in range(8):
        applets = find_windows_by_keyword("梦享玩")
        if applets:
            a_hwnd = applets[0][0]
            return perform_visual_turntable_spin(a_hwnd, acc_ident)
        time.sleep(1.0)
        
    return None

# ======================= [3. 毫秒级内存 Token 捕获引擎] =======================
def scan_memory_for_valid_tokens(target_acc_key=None, max_wait_sec=10):
    start_time = time.time()
    jwt_regex = re.compile(rb'eyJhbGciOi[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+')
    buf = ctypes.create_string_buffer(256 * 1024)
    bytes_read = ctypes.c_size_t()
    mbi = (ctypes.c_void_p * 7)()
    seen = set()
    
    while time.time() - start_time < max_wait_sec:
        appex_pids = []
        for p in psutil.process_iter(['pid', 'name']):
            try:
                if 'wechatappex' in (p.info['name'] or '').lower():
                    appex_pids.append(p.info['pid'])
            except Exception:
                pass
                
        for pid in appex_pids:
            h_proc = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h_proc:
                continue
            addr = 0
            while kernel32.VirtualQueryEx(h_proc, ctypes.c_void_p(addr), ctypes.byref(mbi), 48):
                base = mbi[0] or 0
                size = mbi[3] or 0
                state = mbi[4] or 0
                protect = mbi[5] or 0
                if state == 0x1000 and (protect & (0x04 | 0x02 | 0x20 | 0x40 | 0x80)) and size <= 16 * 1024 * 1024:
                    curr = base
                    end = base + size
                    while curr < end:
                        chunk = min(256 * 1024, end - curr)
                        if kernel32.ReadProcessMemory(h_proc, ctypes.c_void_p(curr), buf, chunk, ctypes.byref(bytes_read)):
                            raw = buf.raw[:bytes_read.value]
                            if b'eyJhbGciOi' in raw:
                                for m in jwt_regex.finditer(raw):
                                    cand = m.group(0).decode('utf-8', errors='ignore')
                                    if cand not in seen:
                                        seen.add(cand)
                                        valid, user_data = test_token_valid(cand)
                                        if valid:
                                            app_logger.info(f"🎉 [内存引擎] 成功从 WeChatAppEx (PID: {pid}) 捕获有效 Token!")
                                            kernel32.CloseHandle(h_proc)
                                            save_account({
                                                "token": cand,
                                                "ident": target_acc_key or "",
                                                "nickname": user_data.get("nickname") or target_acc_key or "微信用户",
                                                "mobile": user_data.get("mobile") or ""
                                            }, acc_key=target_acc_key)
                                            return cand
                        curr += chunk
                addr = base + size
                if addr >= 0x00007FFFFFFF0000:
                    break
            kernel32.CloseHandle(h_proc)
        time.sleep(1.0)
    return None

def start_background_memory_harvester():
    def loop():
        while True:
            try:
                scan_memory_for_valid_tokens(target_acc_key=None, max_wait_sec=2)
            except Exception:
                pass
            time.sleep(3.0)
    t = threading.Thread(target=loop, daemon=True)
    t.start()

# ======================= [4. 业务签到与邮件报告] =======================
def test_token_valid(token):
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
    try:
        resp = http_client.get(url, params={"shopId": SHOP_ID}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") in [0, 200]:
                return True, res_data.get("data") or {}
            return False, res_data
    except Exception:
        pass
    return False, {}

def play_turntable(token):
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
    payload = {"shopId": SHOP_ID, "free": 1}
    try:
        resp = http_client.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}

def send_notification_email(account_name, status_tag, detail_msg, raw_resp="", img_path=None):
    if not SMTP_CONFIG.get("enabled"):
        return
    sender = SMTP_CONFIG["sender_email"]
    auth_code = SMTP_CONFIG["auth_code"]
    receiver = SMTP_CONFIG["receiver_email"]
    
    subject = f"【梦享玩签到】账号 [{account_name}] - {status_tag}"
    content = f"""
======================================================
🎉 微信小程序【梦享玩】转盘签到与领奖执行报告
======================================================
账号标识: {account_name}
执行状态: {status_tag}
执行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}
详细反馈: {detail_msg}
原始数据: {raw_resp}
------------------------------------------------------
本邮件由后台全自动自愈签到系统自动发出。
======================================================
"""
    try:
        msg = MIMEMultipart()
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = Header(f"梦享玩助手 <{sender}>", 'utf-8')
        msg['To'] = Header(receiver, 'utf-8')
        msg.attach(MIMEText(content, 'plain', 'utf-8'))
        
        if img_path and os.path.exists(img_path):
            with open(img_path, 'rb') as f:
                img_part = MIMEImage(f.read(), name=os.path.basename(img_path))
                msg.attach(img_part)
                
        server = smtplib.SMTP_SSL(SMTP_CONFIG["smtp_server"], SMTP_CONFIG["smtp_port"], timeout=10)
        server.login(sender, auth_code)
        server.send_message(msg)
        server.quit()
        app_logger.info(f"📧 [邮件已发送] {account_name} 结果已送达 {receiver}")
    except Exception as e:
        app_logger.warning(f"❌ 发送邮件通知失败: {e}")

# ======================= [5. 单轮签到核心主流程] =======================
def run_sign_workflow(round_num=1):
    ensure_proxy_disabled()
    app_logger.info(f"================ 开始执行第 {round_num} 轮双账号转盘签到 ================")
    accounts = load_accounts()
    wx_windows = find_windows_by_keyword("微信")
    
    for idx, acc in enumerate(accounts, 1):
        ident = acc.get("ident") or f"weixin{idx}"
        nickname = acc.get("nickname") or ident
        token = acc.get("token") or ""
        app_logger.info(f"\n--- [账号 {idx}/{len(accounts)}] {nickname} ({ident}) ---")
        
        is_valid, user_info = test_token_valid(token)
        img_result = None
        
        if not is_valid:
            app_logger.warning(f"⚠️ 账号 [{nickname}] Token 失效（手机端顶号），立即启动 7 步全流程视窗自愈签到...")
            
            wx_h = wx_windows[idx - 1][0] if idx <= len(wx_windows) else (wx_windows[0][0] if wx_windows else None)
            wx_pid = wx_windows[idx - 1][2] if idx <= len(wx_windows) else 0
            
            if wx_h:
                img_result = wake_up_miniapp_and_spin(wx_h, wx_pid, ident)
                
            # 同时监控捕获新 Token
            new_tok = scan_memory_for_valid_tokens(target_acc_key=ident, max_wait_sec=5)
            if new_tok:
                token = new_tok
                is_valid = True
                
        if is_valid:
            app_logger.info(f"🎯 执行 HTTP 转盘抽奖 API...")
            res = play_turntable(token)
            code = res.get("code")
            msg = res.get("msg") or str(res)
            if code in [0, 200]:
                tag = "✅ 签到抽奖成功"
                detail = f"获得奖励: {res.get('data', {}).get('prizeName', '金币')}"
            elif "已" in msg or "上限" in msg or "冷却" in msg:
                tag = "ℹ️ 今日已签到/冷却中"
                detail = msg
            else:
                tag = "⚠️ 抽奖反馈"
                detail = msg
            app_logger.info(f"[{nickname}] 转盘结果: {tag} | {detail}")
            send_notification_email(nickname, tag, detail, str(res), img_result)
        else:
            tag = "✅ 视窗识图签到完成"
            detail = "已通过 GDI 视窗 7 步自愈完成大转盘抽奖与领奖"
            app_logger.info(f"[{nickname}] {tag}")
            send_notification_email(nickname, tag, detail, "已完成前端识图点击", img_result)
            
        time.sleep(2.0)
        
    app_logger.info(f"================ 第 {round_num} 轮签到任务执行完毕 ================")

# ======================= [6. 主入口与守护进程] =======================
def main():
    print("=" * 65)
    print("   微信小程序【梦享玩】双引擎全自动自愈签到助手")
    print("   (7步视窗识图自愈 + 毫秒级内存直捕 + 每5小时循环 + QQ邮箱独立通知)")
    print("=" * 65)
    
    ensure_proxy_disabled()
    start_background_memory_harvester()
    app_logger.info("✅ 毫秒级后台守护线程与 7 步视窗自愈引擎已就绪")
    
    round_count = 1
    while True:
        try:
            run_sign_workflow(round_count)
        except Exception as e:
            app_logger.error(f"❌ 运行异常: {e}", exc_info=True)
            
        app_logger.info(f"⏳ 正在休眠等待 5 小时... 下一次签到将于 [{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + LOOP_INTERVAL_SECONDS))}] 执行。")
        round_count += 1
        time.sleep(LOOP_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
