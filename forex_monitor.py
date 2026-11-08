"""TradingAgents 外汇版 - Telegram 实时价格雷达与策略推送引擎 (Forex Monitor)

功能：
1. 实时轮询主要外汇货币对最新行情（EURUSD, USDJPY, GBPUSD, EURGBP, AUDUSD）。
2. 当价格触碰/进入研报计算出的【高抛做空区】、【低吸做多区】或【目标止盈区】时，自动向手机 Telegram 发送推送。
3. 带有冷却防骚扰机制（同一货币对同一状态不重复轰炸）。
4. 支持测试模式：python forex_monitor.py --test
"""

import os
import sys
import time
import argparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from dotenv import load_dotenv
import requests
import yfinance as yf
from weekend_summary import check_and_trigger_weekend_summary, generate_weekend_summary

# 确保在 Windows 控制台下支持 UTF-8 与 Emoji 正常打印
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 云端健康检查微服务 (供 Render / Railway / Koyeb 等云托管平台 24小时存活心跳使用)
class CloudHealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        html = """
        <html>
        <head><title>TradingAgents Forex Radar Cloud Engine</title></head>
        <body style="font-family:Segoe UI, sans-serif; text-align:center; padding-top:60px; background:#0f172a; color:#f8fafc;">
            <h1 style="color:#10b981;">TradingAgents 外汇行情雷达云端引擎 24/7 正在运行</h1>
            <p style="color:#94a3b8;">系统已锁定 8 大直盘与交叉盘，行情触发将秒级推送至手机 Telegram。</p>
            <p style="font-size:12px; color:#64748b;">Powered by TradingAgents Forex Edition</p>
        </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, format, *args):
        pass

def start_health_server():
    port_str = os.getenv("PORT", "8080")
    try:
        port = int(port_str)
        server = HTTPServer(('0.0.0.0', port), CloudHealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"[云端健康心跳服务已启动] 监听端口: {port}")
    except Exception as e:
        print(f"[健康服务启动跳过]: {e}")

# 加载 .env 环境变量
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8811778752:AAE7QFo-jiOhOUSyJgMRXInLP6eYwF9P_C0").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "409951109").strip()

# 预设基于 TradingAgents 深度研报的外汇交易点位与策略规则
# trigger_type:
#   "SELL_ZONE": 价格反弹进入该区间，触发逢高做空提醒
#   "BUY_ZONE":  价格回落进入该区间，触发逢低做多提醒
#   "TAKE_PROFIT": 价格到达前期目标，触发止盈提醒
MONITOR_RULES = {
    "USDCAD=X": {
        "name": "USD/CAD (美元/加元)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "USDCAD_BUY",
                "type": "BUY_ZONE",
                "condition": lambda p: p <= 1.4095,
                "label": "到达顶底转换回踩多入场带",
                "action": "BUY (回踩顶底转换接多)",
                "entry": "1.4065 - 1.4095",
                "stop_loss": "1.4020",
                "target": "1.4160 (TP1) / 1.4230 (TP2)",
                "logic": "多头主升浪，回踩顶底转换接多。"
            },
            {
                "id": "USDCAD_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p >= 1.4160,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "1.4230 (终极目标 TP2)",
                "logic": "反弹触及 1.4160 关键阻力，建议兑现部分利润并保本推损。"
            },
            {
                "id": "USDCAD_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p <= 1.4020,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "1.4020",
                "target": "-",
                "logic": "跌破 1.4020 平台支撑，主升结构破坏，严格止损离场！"
            }
        ]
    },
    "NZDUSD=X": {
        "name": "NZD/USD (纽元/美元)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "NZDUSD_SELL",
                "type": "SELL_ZONE",
                "condition": lambda p: p >= 0.5575,
                "label": "到达颈线反抽阻力高空入场带",
                "action": "SELL (反抽阻力顺势做空)",
                "entry": "0.5575 - 0.5605",
                "stop_loss": "0.5645",
                "target": "0.5510 (TP1) / 0.5450 (TP2)",
                "logic": "跌破0.5580颈线，反抽阻力顺势空。"
            },
            {
                "id": "NZDUSD_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p <= 0.5510,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "0.5450 (终极目标 TP2)",
                "logic": "空单触及 0.5510 目标位，锁定大部分利润，留仓博弈 0.5450。"
            },
            {
                "id": "NZDUSD_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p >= 0.5645,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "0.5645",
                "target": "-",
                "logic": "突破 0.5645 防守位，下行破位逻辑被破坏，严格止损离场！"
            }
        ]
    },
    "AUDUSD=X": {
        "name": "AUD/USD (澳元/美元)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "AUDUSD_SELL",
                "type": "SELL_ZONE",
                "condition": lambda p: p >= 0.6920,
                "label": "到达容差反弹做空入场带",
                "action": "SELL (逢高分批做空)",
                "entry": "0.6920 - 0.6950",
                "stop_loss": "0.6990",
                "target": "0.6840 (TP1) / 0.6780 (TP2)",
                "logic": "引入容差挂单，逢高分批做空。"
            },
            {
                "id": "AUDUSD_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p <= 0.6840,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "0.6780 (终极目标 TP2)",
                "logic": "触及 0.6840 前低支撑带，分批止盈并移动止损保护盈利。"
            },
            {
                "id": "AUDUSD_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p >= 0.6990,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "0.6990",
                "target": "-",
                "logic": "突破 0.6990 关键阻力，跌势受阻，严格执行止损！"
            }
        ]
    },
    "USDJPY=X": {
        "name": "USD/JPY (美元/日元)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "USDJPY_BUY",
                "type": "BUY_ZONE",
                "condition": lambda p: p <= 158.10,
                "label": "到达利差套息回踩多入场带",
                "action": "BUY (回踩支撑接多)",
                "entry": "157.80 - 158.10",
                "stop_loss": "157.30",
                "target": "158.90 (TP1) / 159.60 (TP2)",
                "logic": "利差套息多单，回踩支撑接多。"
            },
            {
                "id": "USDJPY_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p >= 158.90,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "159.60 (终极目标 TP2)",
                "logic": "反弹测试 158.90 前高强压，建议减仓 50%+，保护利润。"
            },
            {
                "id": "USDJPY_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p <= 157.30,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "157.30",
                "target": "-",
                "logic": "跌破 157.30 平台支撑，利差多头动能减弱，严格止损观望！"
            }
        ]
    },
    "GBPUSD=X": {
        "name": "GBP/USD (英镑/美元)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "GBPUSD_SELL",
                "type": "SELL_ZONE",
                "condition": lambda p: p >= 1.3410,
                "label": "到达扫损后反弹做空入场带",
                "action": "SELL (顺势反弹做空)",
                "entry": "1.3410 - 1.3435",
                "stop_loss": "1.3480",
                "target": "1.3320 (TP1) / 1.3250 (TP2)",
                "logic": "假突破扫损后暴跌，顺势反弹空。"
            },
            {
                "id": "GBPUSD_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p <= 1.3320,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "1.3250 (终极目标 TP2)",
                "logic": "空头到达 1.3320 第一目标，减半仓并严格推保本损。"
            },
            {
                "id": "GBPUSD_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p >= 1.3480,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "1.3480",
                "target": "-",
                "logic": "再度升破 1.3480 阻力区，空头结构瓦解，坚决止损离场！"
            }
        ]
    },
    "EURUSD=X": {
        "name": "EUR/USD (欧元/美元)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "EURUSD_SELL",
                "type": "SELL_ZONE",
                "condition": lambda p: p >= 1.1185,
                "label": "到达反抽供给区高空入场带",
                "action": "SELL (反抽1.1200空)",
                "entry": "1.1185 - 1.1215",
                "stop_loss": "1.1255",
                "target": "1.1100 (TP1) / 1.1030 (TP2)",
                "logic": "供给区承压破位，反抽1.1200空。"
            },
            {
                "id": "EURUSD_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p <= 1.1100,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "1.1030 (终极目标 TP2)",
                "logic": "到达 1.1100 整数关口目标，锁定部分利润并移动止损保护。"
            },
            {
                "id": "EURUSD_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p >= 1.1255,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "1.1255",
                "target": "-",
                "logic": "打穿 1.1255 供给区上限，多头持续强势，必须严格止损！"
            }
        ]
    },
    "USDCHF=X": {
        "name": "USD/CHF (美元/瑞郎)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "USDCHF_BUY",
                "type": "BUY_ZONE",
                "condition": lambda p: p <= 0.8185,
                "label": "到达低位轻仓试多入场带",
                "action": "BUY (低位轻仓试多)",
                "entry": "0.8160 - 0.8185",
                "stop_loss": "0.8120",
                "target": "0.8240 (TP1) / 0.8300 (TP2)",
                "logic": "地缘避险压制，低位轻仓试多。"
            },
            {
                "id": "USDCHF_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p >= 0.8240,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "0.8300 (终极目标 TP2)",
                "logic": "反弹到达 0.8240 阶段压力，建议止盈半仓，剩余仓位看至 0.8300。"
            },
            {
                "id": "USDCHF_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p <= 0.8120,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "0.8120",
                "target": "-",
                "logic": "击穿 0.8120 极值低点，避险强抛压未止，严格止损离场！"
            }
        ]
    },
    "EURGBP=X": {
        "name": "EUR/GBP (欧元/英镑)",
        "status": "ACTIVE",
        "rules": [
            {
                "id": "EURGBP_SELL",
                "type": "SELL_ZONE",
                "condition": lambda p: p >= 0.8365,
                "label": "到达超卖通道阻力做空入场带",
                "action": "SELL (阻力短空)",
                "entry": "0.8365 - 0.8385",
                "stop_loss": "0.8415",
                "target": "0.8315 (TP1) / 0.8280 (TP2)",
                "logic": "深度超卖通道，阻力短空。"
            },
            {
                "id": "EURGBP_TP1",
                "type": "TAKE_PROFIT",
                "condition": lambda p: p <= 0.8315,
                "label": "触及第一目标止盈区 (TP1)",
                "action": "TAKE PROFIT (分批止盈/推保本损)",
                "entry": "-",
                "stop_loss": "保本推损",
                "target": "0.8280 (终极目标 TP2)",
                "logic": "触及 0.8315 超卖下沿，注意减仓并严格保本推损。"
            },
            {
                "id": "EURGBP_SL",
                "type": "STOP_LOSS",
                "condition": lambda p: p >= 0.8415,
                "label": "触及严格防守止损位",
                "action": "STOP LOSS (止损离场/严禁加仓)",
                "entry": "-",
                "stop_loss": "0.8415",
                "target": "-",
                "logic": "升破 0.8415 阻力平台，下行通道被打破，坚决止损！"
            }
        ]
    }
}

# 报警冷却记录（避免重复推送轰炸，默认同一触发点 45 分钟冷却）
ALERT_COOLDOWN_SECONDS = 45 * 60
last_alert_time = {}


def is_weekend_blackout_period() -> tuple[bool, str]:
    """检查当前大马时间 (UTC+8) 是否处于周末闭市拦截黑洞期

    时间窗口：周六 07:00 以后，直到周一 05:00 以前。
    在此期间彻底封死 Telegram 消息发送通道，坚决拒绝任何网络请求与噪音轰炸。
    """
    from datetime import timezone, timedelta
    myt = timezone(timedelta(hours=8))
    now_myt = datetime.now(myt)
    weekday = now_myt.weekday()  # 0=周一, 5=周六, 6=周日
    hm = now_myt.hour * 100 + now_myt.minute

    # 1. 周六 07:00 及以后 (美盘收盘清算完成)
    if weekday == 5 and hm >= 700:
        return True, f"周六闭市停信期 ({now_myt.strftime('%Y-%m-%d %H:%M:%S')} MYT)"
    # 2. 周日全天
    if weekday == 6:
        return True, f"周日全天停信期 ({now_myt.strftime('%Y-%m-%d %H:%M:%S')} MYT)"
    # 3. 周一 05:00 以前 (亚盘开市前)
    if weekday == 0 and hm < 500:
        return True, f"周一开盘前停信期 ({now_myt.strftime('%Y-%m-%d %H:%M:%S')} MYT)"

    return False, ""


def send_telegram_message(message: str) -> bool:
    """发送消息到指定的 Telegram 频道或个人 (含周末彻底硬拦截保护)"""
    # 1. 硬性周末时间拦截：周六 07:00 以后 至 周一 05:00 以前，彻底封死发送通道，坚决不发 HTTP 请求！
    is_blocked, reason = is_weekend_blackout_period()
    if is_blocked:
        print(f"🛑 [周末通道硬拦截] 处于【{reason}】，周末期间坚决拒绝向 Telegram API 发送任何请求！")
        return False

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram 未配置] 请先在 .env 中填入 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        res_data = response.json()
        if res_data.get("ok"):
            print(f"[Telegram 推送成功] {datetime.now().strftime('%H:%M:%S')}")
            return True
        else:
            print(f"[Telegram 推送失败] 错误信息: {res_data.get('description')}")
            return False
    except Exception as e:
        print(f"[Telegram 网络异常] {e}")
        return False


def get_latest_price(symbol: str) -> float | None:
    """获取指定货币对最新即时价格"""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="1m")
        if not df.empty:
            return float(df['Close'].iloc[-1])
        # 回退模式
        df_daily = ticker.history(period="2d")
        if not df_daily.empty:
            return float(df_daily['Close'].iloc[-1])
    except Exception as e:
        print(f"[获取报价失败] {symbol}: {e}")
    return None


def format_alert_message(symbol_name: str, symbol: str, price: float, rule: dict) -> str:
    """格式化 Telegram 推送文本（Markdown 格式）"""
    if rule["type"] == "STOP_LOSS":
        msg = (
            f"🛑 *【AI 首席风控官紧急警报 - 触及止损防守线】*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 *品种*：*{symbol_name}*\n"
            f"💥 *当前触碰价格*：`{price:.5f}`\n"
            f"🛑 *计划止损价*：`{rule['stop_loss']}`\n"
            f"⚡ *执行指令*：*{rule['action']}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *【风控铁律 & 强制冷静指令】*：\n"
            f"1. 严格按计划止损离场，绝不扛单，坚决切断风险！\n"
            f"2. 🔴 *严禁任何形式的逆势补仓、加单摊平！*\n"
            f"3. 市场出现局部异常异动，离开盘面休息 15 分钟，保持冷静！\n"
            f"💡 *风控研判*：{rule['logic']}\n\n"
            f"⏰ 触发时间：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"🤖 _TradingAgents AI 首席风控守护系统_"
        )
        return msg

    type_emoji = "🚨" if rule["type"] == "SELL_ZONE" else ("🟢" if rule["type"] == "BUY_ZONE" else "🎯")
    
    msg = (
        f"{type_emoji} *【外汇投研策略提醒 - {symbol_name}】*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *实时价格*：`{price:.5f}`\n"
        f"📍 *触发状态*：*{rule['label']}*\n"
        f"⚡ *操作建议*：*{rule['action']}*\n"
        f"🎯 *参考入场*：`{rule['entry']}`\n"
        f"🛑 *止损防守*：`{rule['stop_loss']}`\n"
        f"🏁 *目标止盈*：`{rule['target']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 *智能体研判逻辑*：\n{rule['logic']}\n\n"
        f"⏰ 触发时间：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"🤖 _Powered by TradingAgents Forex Edition_"
    )
    return msg


def is_in_liquidity_hunt_period() -> tuple[bool, str]:
    """检查当前时间是否处于高危洗盘/机构刺杀时段 (Judas Swing / Monday Morning)
    
    返回: (是否处于危险时段, 原因说明)
    """
    now = datetime.now()
    weekday = now.weekday()  # 0 = 周一, 4 = 周五, 6 = 周日
    hour_minute = now.hour * 100 + now.minute

    # 1. 每日欧盘开盘刺杀期: 15:30 - 17:15 (冬令时主力假突破、扫流动性最高危时段)
    if 1530 <= hour_minute <= 1715:
        return True, "欧盘开市主力扫损期 (Judas Swing 15:30-17:15)"

    # 2. 周一清晨亚盘探索期: 周一 06:00 - 14:00 (流动性低迷，假动作多，建议等待欧盘后明朗)
    if weekday == 0 and 600 <= hour_minute <= 1400:
        return True, "周一亚盘清淡探索期 (06:00-14:00)"

    return False, ""


def run_monitor(poll_interval: int = 30):
    """启动主监控轮询循环"""
    start_health_server()
    
    active_symbols = [f"{i['name']} ({s})" for s, i in MONITOR_RULES.items() if i.get("status") == "ACTIVE"]
    stopped_symbols = [f"{i['name']} ({s})" for s, i in MONITOR_RULES.items() if i.get("status") == "STOPPED"]

    print("=" * 65)
    print("★ TradingAgents 外汇行情实时雷达 & Telegram 推送服务已启动 ★")
    print(f"轮询间隔: {poll_interval} 秒")
    print(f"🟢 正常追踪中 ({len(active_symbols)} 个品种):")
    for s in active_symbols:
        print(f"   • {s}")
    if stopped_symbols:
        print(f"🛑 已止损熔断/停止 ({len(stopped_symbols)} 个品种，自动跳过):")
        for s in stopped_symbols:
            print(f"   • {s}")
    print("时段风控: ★ 已开启机构洗盘时间锁 (15:30-17:15 欧盘刺杀期自动拦截入场)")
    print(f"Telegram Bot 配置: {'已配置' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '未配置 (仅控制台打印)'}")
    print("=" * 65)

    while True:
        now_time = time.time()

        # 周末闭市自动化复盘检测 (大马时间周六 06:30 美盘收盘结算后自动触发并推送)
        try:
            check_and_trigger_weekend_summary()
        except Exception as e:
            print(f"[周末闭市复盘检测跳过]: {e}")

        # 检查是否处于周末静默停盘期 (周六 07:00 - 周一 05:00)
        is_blackout, blackout_reason = is_weekend_blackout_period()
        if is_blackout:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 💤 处于【{blackout_reason}】，全球外汇市场停盘静默中，停止行情轮询与消息推送 (心跳微服务保持运行)...")
            time.sleep(poll_interval)
            continue

        for symbol, info in list(MONITOR_RULES.items()):
            # 1. 状态机熔断检查：若已熔断停止，直接跳过，不再拉取价格、不再比对、不发通知
            if info.get("status") == "STOPPED":
                continue

            price = get_latest_price(symbol)
            if price is None:
                continue

            print(f"[{datetime.now().strftime('%H:%M:%S')}] {info['name']}: {price:.5f}")

            # 2. 遍历该品种策略规则
            for rule in info["rules"]:
                rule_id = rule["id"]
                if rule["condition"](price):
                    # 【止损熔断机制 Stop Loss Circuit Breaker】
                    # 一旦触碰或打穿止损，立即发送最后一条告警并永久熔断该品种
                    if rule["type"] == "STOP_LOSS":
                        info["status"] = "STOPPED"
                        print("\n" + "!" * 55)
                        print(f"🛑【止损熔断生效】{info['name']} 触碰止损点 {rule['stop_loss']}！监控已永久停止。")
                        print("!" * 55)

                        circuit_breaker_alert = (
                            f"🚨 *【止损熔断生效 · 策略终止通知】*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📊 *品种*：*{info['name']} ({symbol})*\n"
                            f"💥 *当前触碰价格*：`{price:.5f}`\n"
                            f"🛑 *止损点位*：`{rule['stop_loss']}`\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"🚨 *{info['name']} 已触碰止损点 {rule['stop_loss']}，该品种策略已失效，监控已自动永久停止。*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"⚠️ *系统处置*：该品种状态已切换为 `STOPPED`，后续循环已自动剔除，不再比对行情，不再推送任何噪音。\n"
                            f"💡 *风控研判*：{rule['logic']}\n\n"
                            f"⏰ 熔断时间：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
                            f"🤖 _TradingAgents AI 止损熔断守护系统_"
                        )
                        send_telegram_message(circuit_breaker_alert)
                        last_alert_time[rule_id] = now_time
                        break  # 跳出当前品种的其他规则判断

                    # 若是开新仓入场信号 (BUY/SELL)，必须接受时段安全拦截器检验
                    if rule["type"] in ["BUY_ZONE", "SELL_ZONE"]:
                        is_danger, reason = is_in_liquidity_hunt_period()
                        if is_danger:
                            # 处于危险洗盘期，自动拦截开仓信号，防止被刺客行情扫损
                            # 不计入冷却时间，这样 17:15 洗盘结束后若仍符合条件，能第一时间提醒！
                            print(f"  🛡️ [时段风控拦截] {info['name']} 进入价格区，但处于【{reason}】，已自动拦截入场，避开主力洗盘！")
                            continue

                    last_time = last_alert_time.get(rule_id, 0)
                    if now_time - last_time >= ALERT_COOLDOWN_SECONDS:
                        alert_text = format_alert_message(info["name"], symbol, price, rule)
                        print("\n" + "!" * 50)
                        print(f"【触发策略警报】{info['name']} -> {rule['label']}")
                        print("!" * 50)
                        send_telegram_message(alert_text)
                        last_alert_time[rule_id] = now_time

        time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradingAgents Forex Telegram Monitor")
    parser.add_argument("--test", action="store_true", help="发送一条测试通知到 Telegram")
    parser.add_argument("--interval", type=int, default=30, help="行情轮询间隔秒数 (默认 30)")
    parser.add_argument("--weekly-summary", action="store_true", help="立即生成并推送周末闭市复盘报告")
    args = parser.parse_args()

    if args.weekly_summary:
        print("正在手动执行周末闭市复盘...")
        generate_weekend_summary(send_telegram=True, save_files=True)
    elif args.test:
        test_msg = (
            "🔔 *【TradingAgents 外汇版 - Telegram 推送测试】*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "恭喜！您的 Telegram 推送通知已成功打通！\n"
            "当外汇品种（EUR/USD, USD/JPY, EUR/GBP, GBP/USD 等）触及关键做空/做多阻力位或目标位时，您将第一时间收到此类策略提醒。\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 测试时间：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
        )
        print("正在发送测试消息到 Telegram...")
        success = send_telegram_message(test_msg)
        if success:
            print("测试成功！手机端请查看 Telegram 消息。")
        else:
            print("测试失败，请检查 .env 中的 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 是否正确。")
    else:
        run_monitor(poll_interval=args.interval)
