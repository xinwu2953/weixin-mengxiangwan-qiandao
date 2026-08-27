#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信小程序【梦享玩】全自动签到与自愈脚本 (双账号支持 + 定时循环 + 独立QQ邮箱通知)
- 智能内嵌抓包：拦截 Bearer Token 及 RefreshToken 并自动绑定到对应账号
- 定向抓包放行：仅拦截目标接口，官方资源直连，避免 SSL 冲突
- 双账号原生界面自愈：自动清理旧小程序进程冷启动，模拟搜索唤醒小程序，无系统权限弹窗
- 智能签到解析：精准识别【已完成全部签到 / 5小时冷却 / 获得游戏币 / 实物奖励】
- 当日上限停签：当天满2次自动记录，本日后续巡检智能跳过
- 邮件独立发送：每个账号单独发送美化 HTML 邮件通知
"""

import os
import sys
import time
import json
import socket
import logging
import asyncio
import threading
import smtplib
import subprocess
import ctypes
from ctypes import wintypes
import winreg
import atexit
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

# ======================= [业务与代理配置] =======================
APP_ID = "wx44a67f9e199a46d0"        # 小程序 AppID
SHOP_ID = 4                           # 店铺 ID
PROXY_PORT = 8888                     # 本地代理端口
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

# 创建独立的 HTTP 请求 Session，绕过系统代理直接请求后端 API
http_client = requests.Session()
http_client.trust_env = False
http_client.verify = False

# 独立日志记录器
app_logger = logging.getLogger("wechat_sign_app")
app_logger.setLevel(logging.INFO)
app_logger.propagate = False
if not app_logger.handlers:
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    app_logger.addHandler(sh)

# 全局自愈目标账号与文件锁
current_relogin_target_account = None
db_lock = threading.Lock()

# ======================= [端口检查与单实例保护] =======================
def clean_port_conflict(port):
    """检测端口是否被占用，若被占用则尝试安全释放"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                app_logger.warning(f"⚠️ 检测到端口 {port} 已被占用，正在查找并终止旧进程...")
                cur_pid = os.getpid()
                for conn in psutil.net_connections():
                    if conn.laddr and conn.laddr.port == port and conn.pid and conn.pid != cur_pid:
                        try:
                            proc = psutil.Process(conn.pid)
                            app_logger.info(f"终止占用端口 {port} 的旧进程: {proc.name()} (PID: {conn.pid})")
                            proc.kill()
                        except Exception:
                            pass
                time.sleep(1)
    except Exception as e:
        app_logger.error(f"端口冲突检测异常: {e}")

# ======================= [Windows API / 结构体定义] =======================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ('length', ctypes.wintypes.UINT),
        ('flags', ctypes.wintypes.UINT),
        ('showCmd', ctypes.wintypes.UINT),
        ('ptMinPosition', ctypes.wintypes.POINT),
        ('ptMaxPosition', ctypes.wintypes.POINT),
        ('rcNormalPosition', ctypes.wintypes.RECT)
    ]

def set_system_proxy(enable=True, host="127.0.0.1", port=8888):
    """设置或清除 Windows 系统代理 (IE/WinINet)"""
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
            app_logger.info(f"🌐 Windows 系统代理已设置为: {host}:{port}")
        else:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            app_logger.info("🌐 Windows 系统代理已还原 (禁用)")
        winreg.CloseKey(key)

        internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
        internet_set_option(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        internet_set_option(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception as e:
        app_logger.error(f"设置系统代理异常: {e}")

def get_main_wechat_windows():
    """高精度定位运行中微信实例的主聊天窗口 (过滤托盘及辅助子窗口)"""
    try:
        h_desk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
    except Exception:
        pass

    pid_windows = {}
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    
    def enum_cb(hwnd, lparam):
        try:
            if not user32.IsWindow(hwnd):
                return True
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0:
                return True
            
            buf_cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf_cls, 256)
            cls = buf_cls.value
            
            if cls in ["Qt51514QWindowIcon", "WeChatMainWndForPC"] and user32.GetParent(hwnd) == 0:
                wp = WINDOWPLACEMENT()
                wp.length = ctypes.sizeof(wp)
                user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
                r = wp.rcNormalPosition
                area = (r.right - r.left) * (r.bottom - r.top)
                # 过滤小于100,000像素的托盘弹窗，精准锁定主窗口
                if area > 100000:
                    if pid.value not in pid_windows or area > pid_windows[pid.value]['area']:
                        pid_windows[pid.value] = {
                            'hwnd': hwnd,
                            'pid': pid.value,
                            'cls': cls,
                            'area': area,
                            'rect': (r.left, r.top, r.right, r.bottom)
                        }
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(enum_cb)
    user32.EnumWindows(cb, 0)
    return pid_windows

def click_screen(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.1)

def find_applet_window():
    """查找当前运行中梦享玩小程序窗口"""
    h_desk = user32.OpenInputDesktop(0, False, 0x01FF)
    if not h_desk:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
    user32.SetThreadDesktop(h_desk)
    
    applet_hwnds = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(h, lparam):
        if user32.IsWindowVisible(h):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            try:
                p = psutil.Process(pid.value)
                if 'wechatappex' in p.name().lower():
                    buf_title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(h, buf_title, 512)
                    rect = wintypes.RECT()
                    user32.GetWindowRect(h, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h_val = rect.bottom - rect.top
                    if w > 200 and h_val > 200:
                        applet_hwnds.append((h, pid.value, buf_title.value, rect))
            except:
                pass
        return True
    
    user32.EnumDesktopWindows(h_desk, WNDENUMPROC(enum_cb), 0)
    return applet_hwnds

def activate_and_open_miniapp(hwnd, pid, index, acc_key=""):
    """激活指定微信主窗口并通过原生搜索唤醒打开小程序，自动模拟授权登录"""
    try:
        set_system_proxy(False)
        cur_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        if cur_thread != target_thread and target_thread != 0:
            user32.AttachThreadInput(cur_thread, target_thread, True)
        
        # 统一恢复并固定窗口位置，避免坐标错位
        user32.ShowWindow(hwnd, 9) # SW_RESTORE
        user32.ShowWindow(hwnd, 5) # SW_SHOW
        user32.SetWindowPos(hwnd, -1, 100, 100, 1000, 700, 0x0040) # HWND_TOP + SWP_SHOWWINDOW
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        
        if cur_thread != target_thread and target_thread != 0:
            user32.AttachThreadInput(cur_thread, target_thread, False)
        
        time.sleep(0.6)
        app_logger.info(f"📱 正在为第 {index} 个微信 [{acc_key}] (HWND: {hwnd}, PID: {pid}) 定位并拉起'梦享玩'小程序...")
        
        # 先按 Esc 关闭任何已存在的遮罩或弹窗
        user32.keybd_event(0x1B, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x1B, 0, 2, 0)
        time.sleep(0.2)
        
        # 1. 快捷键 Ctrl + F 聚焦全局搜索框
        user32.keybd_event(0x11, 0, 0, 0) # Ctrl
        user32.keybd_event(0x46, 0, 0, 0) # F
        time.sleep(0.05)
        user32.keybd_event(0x46, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.4)
        
        # 2. 全选并清空可能残留的旧搜索词
        user32.keybd_event(0x11, 0, 0, 0) # Ctrl
        user32.keybd_event(0x41, 0, 0, 0) # A
        time.sleep(0.05)
        user32.keybd_event(0x41, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.1)
        user32.keybd_event(0x2E, 0, 0, 0) # Del
        time.sleep(0.05)
        user32.keybd_event(0x2E, 0, 2, 0)
        time.sleep(0.2)
        
        # 3. 剪贴板输入“梦享玩”
        pyperclip.copy("梦享玩")
        user32.keybd_event(0x11, 0, 0, 0) # Ctrl
        user32.keybd_event(0x56, 0, 0, 0) # V
        time.sleep(0.08)
        user32.keybd_event(0x56, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(1.2) # 等待微信搜索下拉面板渲染
        
        # 4. 回车确认打开小程序直达项
        user32.keybd_event(0x0D, 0, 0, 0) # Enter
        time.sleep(0.08)
        user32.keybd_event(0x0D, 0, 2, 0)
        time.sleep(3.5)
        
        # 5. 定位并前置小程序窗口，执行界面授权触控
        applets = find_applet_window()
        if applets:
            app_h = applets[0][0]
            user32.ShowWindow(app_h, 9)
            user32.ShowWindow(app_h, 5)
            user32.SetWindowPos(app_h, -1, 200, 100, 500, 800, 0x0040)
            user32.SetForegroundWindow(app_h)
            time.sleep(0.8)
            
            app_logger.info(f"🖱️ 正在小程序界面模拟点击触发授权登录...")
            # 点击“我的”触发授权检查 (screen x=200+350=550, y=100+730=830)
            user32.SetCursorPos(550, 830)
            time.sleep(0.1)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.08)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(1.0)
            
            # 点击“每日转盘” (screen x=200+265=465, y=100+360=460)
            user32.SetCursorPos(465, 460)
            time.sleep(0.1)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.08)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(1.5)
    except Exception as e:
        app_logger.error(f"激活微信窗口失败: {e}")

def trigger_dual_wechat_relogin(target_acc_key=None):
    """
    自愈唤醒微信并抓包最新 Token
    支持指定单个账号自愈（如 'weixin252121438' 或 'weixin2'），或全部自愈
    """
    global current_relogin_target_account
    set_system_proxy(False)
    windows_map = get_main_wechat_windows()
    if not windows_map:
        app_logger.error("❌ 未检测到运行中的微信主窗口，请确保微信多开正常挂在后台！")
        return False
        
    sorted_pids = sorted(windows_map.keys())
    app_logger.info(f"🔍 找到 {len(sorted_pids)} 个微信主窗口实例...")
    
    target_tasks = []
    if target_acc_key == "weixin252121438" or target_acc_key == "1":
        if len(sorted_pids) >= 1:
            target_tasks.append((sorted_pids[0], windows_map[sorted_pids[0]], 1, "weixin252121438"))
    elif target_acc_key == "weixin2" or target_acc_key == "2":
        if len(sorted_pids) >= 2:
            target_tasks.append((sorted_pids[1], windows_map[sorted_pids[1]], 2, "weixin2"))
    else:
        # 默认自愈所有账号
        acc_keys = ["weixin252121438", "weixin2"]
        for idx, pid in enumerate(sorted_pids[:2], start=1):
            target_tasks.append((pid, windows_map[pid], idx, acc_keys[idx-1] if idx-1 < len(acc_keys) else f"acc_{idx}"))
            
    for pid, info, idx, acc_key in target_tasks:
        current_relogin_target_account = acc_key
        
        # 1. 确保网络代理禁用，避免小程序白屏
        set_system_proxy(False)
                    
        # 2. 通过主微信窗口定位并拉起小程序
        activate_and_open_miniapp(info['hwnd'], pid, idx, acc_key)
        
        app_logger.info(f"⏳ 正在为账号 {idx} [{acc_key}] 监控最新 Token (最多等待 15 秒)...")
        for _ in range(15):
            time.sleep(1)
            accs = load_accounts()
            cur_tok = accs.get(acc_key, {}).get("token")
            if test_token_valid(cur_tok):
                app_logger.info(f"✅ 账号 {idx} [{acc_key}] 成功自愈获取有效 Token！")
                break
                
        # 抓取完成后关闭小程序窗口
        applets = find_applet_window()
        for ah, apid, atitle, arect in applets:
            try:
                user32.PostMessageW(ah, 0x0010, 0, 0) # WM_CLOSE
            except:
                pass
        time.sleep(1.5)
        
    current_relogin_target_account = None
    return True

# ======================= [1. 邮件发送模块 (带多轮重试 & 智能排版)] =======================
def send_email_report(account_index, nickname, mobile, success, status_desc, prize_info, raw_msg):
    """
    给指定邮箱发送独立的签到结果邮件 (带 3 次网络重试机制)
    """
    if not SMTP_CONFIG.get("enabled"):
        return
    
    # 智能标签与色彩
    if success:
        status_tag = "✅签到成功"
        theme_color = "#2e7d32"
    elif "今日已完成" in status_desc or "上限" in status_desc:
        status_tag = "🛑今日已达上限"
        theme_color = "#1565c0"
    elif "冷却" in status_desc:
        status_tag = "⏳冷却等待中"
        theme_color = "#f57c00"
    else:
        status_tag = "⚠️签到反馈"
        theme_color = "#d32f2f"
        
    tail_mobile = mobile[-4:] if len(mobile) >= 4 else mobile
    subject = f"【{status_tag}】账号{account_index}：{nickname}({tail_mobile}) - {prize_info}"
    now_time = time.strftime("%Y-%m-%d %H:%M:%S")
    
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; background-color: #f4f6f9; padding: 20px;">
        <div style="max-width: 600px; margin: auto; background-color: #ffffff; padding: 25px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08);">
            <h2 style="color: {theme_color}; border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top: 0;">
                🎉 微信账号 {account_index} 转盘签到提醒
            </h2>
            <table style="width: 100%; border-collapse: collapse; margin-top: 15px;">
                <tr>
                    <td style="padding: 10px; color: #666; width: 120px; border-bottom: 1px solid #f0f0f0;"><b>账号序号：</b></td>
                    <td style="padding: 10px; font-weight: bold; border-bottom: 1px solid #f0f0f0;">第 {account_index} 个微信账号</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #666; border-bottom: 1px solid #f0f0f0;"><b>用户昵称：</b></td>
                    <td style="padding: 10px; font-weight: bold; border-bottom: 1px solid #f0f0f0;">{nickname}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #666; border-bottom: 1px solid #f0f0f0;"><b>手机号码：</b></td>
                    <td style="padding: 10px; border-bottom: 1px solid #f0f0f0;">{mobile}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #666; border-bottom: 1px solid #f0f0f0;"><b>签到状态：</b></td>
                    <td style="padding: 10px; color: {theme_color}; font-weight: bold; border-bottom: 1px solid #f0f0f0;">{status_desc}</td>
                </tr>
                <tr style="background-color: #fff8e1;">
                    <td style="padding: 10px; color: #666; border-bottom: 1px solid #f0f0f0;"><b>获得奖励/提示：</b></td>
                    <td style="padding: 10px; font-size: 15px; color: #e65100; font-weight: bold; border-bottom: 1px solid #f0f0f0;">{prize_info}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #666; border-bottom: 1px solid #f0f0f0;"><b>服务器响应：</b></td>
                    <td style="padding: 10px; color: #555; border-bottom: 1px solid #f0f0f0;">{raw_msg}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #666;"><b>执行时间：</b></td>
                    <td style="padding: 10px; color: #888;">{now_time}</td>
                </tr>
            </table>
            <hr style="border: none; border-top: 1px solid #eee; margin-top: 20px;">
            <p style="font-size: 12px; color: #999; text-align: center;">此邮件由后台全自动签到助手发送，每 5 小时自动巡检执行一次。</p>
        </div>
    </body>
    </html>
    """
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            msg = MIMEMultipart()
            msg["From"] = Header(f"微信双开签到助手 <{SMTP_CONFIG['sender_email']}>", "utf-8")
            msg["To"] = Header(SMTP_CONFIG["receiver_email"], "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg.attach(MIMEText(html_content, "html", "utf-8"))
            server = smtplib.SMTP_SSL(SMTP_CONFIG["smtp_server"], SMTP_CONFIG["smtp_port"], timeout=15)
            server.login(SMTP_CONFIG["sender_email"], SMTP_CONFIG["auth_code"])
            server.sendmail(SMTP_CONFIG["sender_email"], [SMTP_CONFIG["receiver_email"]], msg.as_string())
            server.quit()
            app_logger.info(f"📧 [邮件已发送] 账号 {account_index}({nickname}) 结果已送达 {SMTP_CONFIG['receiver_email']}")
            return True
        except Exception as e:
            app_logger.warning(f"⚠️ 发送邮件第 {attempt}/{MAX_NETWORK_RETRIES} 次尝试失败: {e}")
            if attempt < MAX_NETWORK_RETRIES:
                time.sleep(2)
            else:
                app_logger.error(f"❌ 邮件发送最终失败: {e}")
    return False

# ======================= [2. 账号数据持久化与多源同步] =======================
def load_accounts():
    db = {}
    with db_lock:
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    db = json.load(f)
            except Exception:
                db = {}
                
        # 兼容外部 token.json 文件
        for acc_key, ext_path in EXTERNAL_TOKEN_FILES.items():
            if os.path.exists(ext_path):
                try:
                    with open(ext_path, "r", encoding="utf-8") as f:
                        ext_data = json.load(f)
                        rt = ext_data.get("refresh_token") or ext_data.get("refreshToken")
                        tok = ext_data.get("token")
                        if rt or tok:
                            if acc_key not in db:
                                db[acc_key] = {
                                    "mobile": acc_key,
                                    "nickname": acc_key,
                                    "token": tok or "",
                                    "refreshToken": rt or "",
                                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
                                }
                            else:
                                if rt:
                                    db[acc_key]["refreshToken"] = rt
                                if tok and not db[acc_key].get("token"):
                                    db[acc_key]["token"] = tok
                except Exception:
                    pass

    return db

def save_account(user_data, acc_key=None):
    """保存或更新单个账号信息，并将最新 Token 同步回写 external token.json"""
    global current_relogin_target_account
    with db_lock:
        db = {}
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    db = json.load(f)
            except Exception:
                db = {}

        target_key = acc_key or current_relogin_target_account or user_data.get("ident") or user_data.get("mobile")
        
        # 如果未指定 target_key，优先匹配现有账号列表中 Token 无效的账号
        if not target_key or target_key not in db:
            for k, v in db.items():
                if not test_token_valid(v.get("token")):
                    target_key = k
                    break
                    
        # 若仍无法匹配，回退到首个账号
        if not target_key:
            keys = list(db.keys())
            target_key = keys[0] if keys else "weixin252121438"

        existing = db.get(target_key, {})
        new_token = user_data.get("token") or existing.get("token", "")
        new_refresh = user_data.get("refreshToken") or user_data.get("refresh_token") or existing.get("refreshToken", "")
        daily_date = user_data.get("daily_completed_date") or existing.get("daily_completed_date", "")
        
        db[target_key] = {
            "mobile": existing.get("mobile", target_key),
            "nickname": existing.get("nickname", target_key),
            "ident": existing.get("ident", target_key),
            "token": new_token,
            "refreshToken": new_refresh,
            "daily_completed_date": daily_date,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
            
        # 同步回写 external token.json
        if target_key in EXTERNAL_TOKEN_FILES:
            try:
                ext_path = EXTERNAL_TOKEN_FILES[target_key]
                os.makedirs(os.path.dirname(ext_path), exist_ok=True)
                with open(ext_path, "w", encoding="utf-8") as ef:
                    json.dump({
                        "refresh_token": db[target_key]["refreshToken"],
                        "token": db[target_key]["token"]
                    }, ef, indent=2)
            except Exception:
                pass
                
        app_logger.info(f"🎉 [数据持久化] 成功更新保存账号 [{db[target_key].get('nickname', target_key)} ({target_key})] 最新 Token 数据！")

def handle_intercepted_token(token, refresh_token=None):
    """处理代理抓包捕获到的 Token 并持久化存储"""
    if not token:
        return
    save_account({
        "token": token,
        "refreshToken": refresh_token
    })

# ======================= [3. 内嵌 Mitmproxy 抓包代理服务] =======================
from mitmproxy import http, options
from mitmproxy.tools.dump import DumpMaster

class MiniAppInterceptor:
    def request(self, flow: http.HTTPFlow) -> None:
        if "mongoose.liangjingkeji.com" in flow.request.pretty_url:
            tok = flow.request.headers.get("token", "") or flow.request.headers.get("Authorization", "")
            if tok.startswith("Bearer "):
                tok = tok.split("Bearer ")[1].strip()
            if tok and len(tok) >= 20:
                app_logger.info(f"🎯 [抓包拦截] 从请求头捕获到有效 Token: {tok[:10]}***")
                threading.Thread(target=handle_intercepted_token, args=(tok,), daemon=True).start()

    def response(self, flow: http.HTTPFlow) -> None:
        if "mongoose.liangjingkeji.com" in flow.request.pretty_url:
            try:
                res = json.loads(flow.response.text)
                data = res.get("data")
                tok = None
                rt = None
                if isinstance(data, dict):
                    tok = data.get("token") or data.get("accessToken")
                    rt = data.get("refreshToken")
                elif isinstance(data, str) and len(data) >= 20:
                    tok = data
                    
                if tok:
                    app_logger.info(f"🎯 [抓包拦截] 从服务端响应捕获到全新 Token: {tok[:10]}***")
                    threading.Thread(target=handle_intercepted_token, args=(tok, rt), daemon=True).start()
            except Exception:
                pass

async def _run_proxy_async(port):
    try:
        opts = options.Options(
            mode=[f"regular@{port}", "local:WeChatAppEx.exe", "local:Weixin.exe"],
            listen_host="127.0.0.1",
            listen_port=port,
            ssl_insecure=True,
        )
    except Exception:
        opts = options.Options(
            listen_host="127.0.0.1",
            listen_port=port,
            ssl_insecure=True,
        )
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.addons.add(MiniAppInterceptor())
    app_logger.info(f"✅ 内嵌抓包代理服务已就绪 (监听端口: {port})")
    await master.run()

def start_embedded_proxy():
    clean_port_conflict(PROXY_PORT)
    set_system_proxy(enable=True, host="127.0.0.1", port=PROXY_PORT)
    try:
        asyncio.run(_run_proxy_async(PROXY_PORT))
    except Exception as e:
        app_logger.error(f"内嵌代理异常退出: {e}")
    finally:
        set_system_proxy(enable=False)

# ======================= [4. 业务请求与自动签到 (带智能响应解析 & 当日上限停签)] =======================
def test_token_valid(token):
    """测试 Token 是否有效 (带多次网络重试机制)"""
    if not token:
        return False
    url = f"{BASE_URL}/turntable/paying/info"
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://servicewechat.com/wx44a67f9e199a46d0/221/page-frame.html"
    }
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            r = http_client.get(url, params={"shopId": SHOP_ID}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if r.status_code == 200:
                res = r.json()
                return res.get("code") in [0, 200]
            elif r.status_code in [401, 403]:
                return False
        except Exception as e:
            app_logger.warning(f"⚠️ 测试 Token 接口第 {attempt}/{MAX_NETWORK_RETRIES} 次网络异常: {e}")
            if attempt < MAX_NETWORK_RETRIES:
                time.sleep(2)
    return False

def try_refresh_token(refresh_token):
    """尝试使用 refreshToken 静默刷新 token (带多次网络重试机制)"""
    if not refresh_token:
        return None
    url = f"{BASE_URL}/token/refresh"
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "xweb_xhr": "1",
        "Referer": "https://servicewechat.com/wx44a67f9e199a46d0/221/page-frame.html"
    }
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            r = http_client.post(url, data={"refreshToken": refresh_token}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if r.status_code == 200:
                res = r.json()
                if res.get("code") == 200 and "data" in res:
                    return res["data"]
                else:
                    app_logger.warning(f"⚠️ 刷新 Token 服务端响应: {res.get('msg')}")
                    return None
            elif r.status_code in [401, 403, 409]:
                app_logger.warning(f"⚠️ RefreshToken 已在服务端失效 (状态码 {r.status_code})")
                return None
        except Exception as e:
            app_logger.warning(f"⚠️ 刷新 Token 第 {attempt}/{MAX_NETWORK_RETRIES} 次网络超时/重试: {e}")
            if attempt < MAX_NETWORK_RETRIES:
                time.sleep(2)
    return None

def play_turntable(token):
    """执行转盘签到抽奖 (带多次网络重试机制)"""
    url = f"{BASE_URL}/turntable/play/mp/new"
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://servicewechat.com/wx44a67f9e199a46d0/221/page-frame.html",
        "xweb_xhr": "1"
    }
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            resp = http_client.post(url, data={"shopId": SHOP_ID}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            return resp.json()
        except Exception as e:
            app_logger.warning(f"⚠️ 转盘抽奖第 {attempt}/{MAX_NETWORK_RETRIES} 次网络异常: {e}")
            if attempt < MAX_NETWORK_RETRIES:
                time.sleep(2)
            else:
                return {"code": -1, "msg": f"网络重试耗尽: {e}"}

def parse_prize_info(result):
    """
    智能解析转盘抽奖响应
    返回: (success_bool, status_desc, prize_desc, raw_msg, is_daily_finished)
    """
    code = result.get("code")
    data = result.get("data")
    msg = str(result.get("msg") or "")
    
    # 1. 抽奖成功
    if code in [200, 0]:
        if isinstance(data, dict):
            coin = data.get("coin") or data.get("num") or data.get("gameCoin") or data.get("score")
            name = data.get("name") or data.get("prizeName") or data.get("title")
            if coin is not None:
                return True, "🎉 抽奖成功", f"获得 {coin} 个游戏币 ({name if name else ''})", str(result), False
            elif name:
                return True, "🎉 抽奖成功", f"获得奖励: {name}", str(result), False
        return True, "🎉 抽奖成功", "获得转盘奖励 (已入账)", str(result), False
        
    # 2. 当天抽奖次数已用完 (已签满 2 次) -> 触发当天停签标记
    if "次数已用完" in msg or "已用完" in msg:
        return False, "🛑 今日已完成全部签到", "今日免费抽奖次数已耗尽 (停止本日后续签到)", msg, True
        
    # 3. 5小时冷却间隔中 (未满5小时)
    if "5个" in msg or "5小时" in msg or "未满" in msg or "冷却" in msg:
        return False, "⏳ 抽奖冷却中", "未满 5 小时冷却间隔 (下次巡检自动补签)", msg, False
        
    # 4. 登录失效 (被手机端顶号 / refreshToken失效)
    if code in [401, 403] or "登录" in msg or "refreshToken" in msg or "token" in msg:
        return False, "⚠️ 登录已失效 (手机端顶号)", "被手机端登录顶掉Session，请在电脑微信点开梦享玩完成授权", msg, False
        
    # 5. 其他未成功情况
    return False, "⚠️ 签到未成功", "未获取到奖励", msg if msg else str(result), False

def run_sign_workflow(round_count):
    app_logger.info(f"================ 开始执行第 {round_count} 轮双账号转盘签到 ================")
    accounts = load_accounts()
    today_str = time.strftime("%Y-%m-%d")
    
    # 检查账号数量及有效性
    for acc_key, info in list(accounts.items()):
        # 如果当天已经签满，无需强制重登
        if info.get("daily_completed_date") == today_str:
            continue
            
        token = info.get("token")
        if not test_token_valid(token):
            app_logger.warning(f"⚠️ 账号 [{info.get('nickname', acc_key)}] Token 无效，尝试静默刷新...")
            refreshed_data = try_refresh_token(info.get("refreshToken"))
            if refreshed_data and refreshed_data.get("token"):
                info["token"] = refreshed_data.get("token")
                if refreshed_data.get("refreshToken"):
                    info["refreshToken"] = refreshed_data.get("refreshToken")
                save_account(info, acc_key=acc_key)
                app_logger.info(f"✅ 账号 [{info.get('nickname', acc_key)}] 静默刷新 Token 成功！")
            else:
                app_logger.warning(f"⚠️ 账号 [{info.get('nickname', acc_key)}] 静默刷新失败，正在触发窗口自愈重登...")
                trigger_dual_wechat_relogin(acc_key)

    accounts = load_accounts()
    if not accounts:
        app_logger.error("❌ 未能获取到任何有效账号，本次签到跳过。")
        return

    # 遍历每个账号执行签到并独立发信
    for idx, (acc_key, info) in enumerate(accounts.items(), start=1):
        nickname = info.get("nickname", f"用户{idx}")
        mobile = str(info.get("mobile", acc_key))
        token = info.get("token")
        
        # 检查该账号今天是否已经完成全部抽奖
        if info.get("daily_completed_date") == today_str:
            app_logger.info(f"⏭️ 账号 {idx} [{nickname}] 今日抽奖次数已用完，跳过本日后续执行。")
            continue
            
        if not token:
            app_logger.warning(f"⚠️ 账号 {idx} [{nickname}] 缺少 Token，跳过签到。")
            continue
            
        app_logger.info(f"🎯 正在为账号 {idx} [{nickname} ({mobile})] 执行转盘抽奖...")
        result = play_turntable(token)
        
        # 如果抽奖返回“请登录后再操作”，触发就地自愈再重试一次
        if result.get("code") in [401, 403] or "登录" in str(result.get("msg", "")):
            app_logger.warning(f"⚠️ 账号 {idx} [{nickname}] 抽奖反馈需重新登录，立即触发窗口自愈重试...")
            trigger_dual_wechat_relogin(acc_key)
            fresh_accs = load_accounts()
            if fresh_accs.get(acc_key, {}).get("token"):
                token = fresh_accs[acc_key]["token"]
                result = play_turntable(token)
                
        success, status_desc, prize_desc, raw_msg, is_daily_finished = parse_prize_info(result)
        app_logger.info(f"[{nickname}] 转盘结果: {status_desc} | {prize_desc} | 原始响应: {raw_msg}")
        
        # 如果当天次数用完，记录完成日期以停止今天后续的请求
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

# ======================= [5. 程序主入口 & 定时调度] =======================
def main():
    print("=" * 65)
    print("   微信小程序双开全自动自愈签到助手 (每5小时循环 + QQ邮箱独立通知)   ")
    print("=" * 65)
    
    # 确保网络环境干净，绝不残留代理导致小程序白屏
    set_system_proxy(False)
    atexit.register(lambda: set_system_proxy(False))
    
    # 1. 启动内嵌抓包代理
    proxy_thread = threading.Thread(target=start_embedded_proxy, daemon=True)
    proxy_thread.start()
    time.sleep(2)
    
    round_idx = 1
    
    # 2. 循环执行
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
