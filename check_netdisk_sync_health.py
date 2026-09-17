"""
网盘同步健康检查脚本

原理：在本地同步目录写入一个带唯一标记的临时文件，然后轮询 Google Drive
远端，看这个标记是否在规定时间内出现。如果没出现，说明自动同步（watchdog
监控 + rclone sync）没有正常工作，通过邮件发出告警。

用法：
  作为常驻后台进程运行（一直循环，每天固定时间检查一次，时间由 .env 里
  SYNC_ALERT_CHECK_TIME 配置，默认 09:00）：
    python Desktop-check_netdisk_sync_health.py

  临时手动跑一次检查（跑完立刻退出，不进入循环，方便随时测试）：
    python Desktop-check_netdisk_sync_health.py --once

  只测试邮件能不能发出去（不做同步检测，只发一封测试邮件）：
    python Desktop-check_netdisk_sync_health.py --test-email
"""

import os
import ast
import operator
import time
import uuid
import argparse
import subprocess
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

from dotenv import load_dotenv

# 加载 .env 配置（脚本所在目录下的 .env）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


class LineLimitedFileHandler(logging.Handler):
    """固定文件名，最新的日志插在文件最前面，超过 max_lines 行时删除最后面（最旧）的记录"""

    def __init__(self, filename, max_lines=300, encoding='utf-8'):
        super().__init__()
        self.baseFilename = os.path.abspath(filename)
        self.max_lines = max_lines
        self.encoding = encoding

    def emit(self, record):
        try:
            msg = self.format(record)
            self.acquire()
            try:
                if os.path.exists(self.baseFilename):
                    with open(self.baseFilename, 'r', encoding=self.encoding, errors='replace') as f:
                        existing_lines = f.readlines()
                else:
                    existing_lines = []
                new_lines = [msg + '\n'] + existing_lines
                if len(new_lines) > self.max_lines:
                    new_lines = new_lines[:self.max_lines]
                with open(self.baseFilename, 'w', encoding=self.encoding) as f:
                    f.writelines(new_lines)
            finally:
                self.release()
        except Exception:
            self.handleError(record)


# 配置日志：固定写到脚本所在目录下的 check_netdisk_sync_health.log，最多保留300行
log_file = os.path.join(BASE_DIR, "check_netdisk_sync_health.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        LineLimitedFileHandler(log_file, max_lines=300),
        logging.StreamHandler()
    ]
)

# 与主同步脚本保持一致
source_path = r"D:\files\myNetdisk"
destination_path = 'gdrive:/sync/Desktop/my_netdisk'

MARKER_NAME = "_sync_healthcheck.txt"
LOCAL_MARKER = os.path.join(source_path, MARKER_NAME)
REMOTE_MARKER = f"{destination_path}/{MARKER_NAME}"

_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_arithmetic_node(node):
    if isinstance(node, ast.Expression):
        return _eval_arithmetic_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPERATORS:
        return _SAFE_OPERATORS[type(node.op)](
            _eval_arithmetic_node(node.left), _eval_arithmetic_node(node.right)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPERATORS:
        return _SAFE_OPERATORS[type(node.op)](_eval_arithmetic_node(node.operand))
    raise ValueError(f"不支持的表达式节点: {ast.dump(node)}")


def parse_int_env(name: str, default: int) -> int:
    """从环境变量读取整数配置，支持简单算术表达式（例如 60*1、24*60*60）"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = _eval_arithmetic_node(ast.parse(raw, mode='eval').body)
        return int(value)
    except Exception as e:
        logging.warning(f"解析环境变量 {name}={raw!r} 失败（{e}），使用默认值 {default}")
        return default


POLL_INTERVAL_SECONDS = parse_int_env("SYNC_ALERT_POLL_INTERVAL_SECONDS", 30)
CHECK_TIMEOUT_SECONDS = parse_int_env("SYNC_ALERT_CHECK_TIMEOUT_SECONDS", 20 * 60)
CHECK_TIME_STR = os.environ.get("SYNC_ALERT_CHECK_TIME", "09:00")  # 后台常驻模式下，每天几点检查


def next_daily_run_time(check_time_str: str, now: datetime = None) -> datetime:
    """计算下一次该跑检查的时间点（每天固定时刻，如果今天这个时刻已经过了就排到明天）"""
    now = now or datetime.now()
    try:
        hour, minute = (int(part) for part in check_time_str.strip().split(":"))
    except Exception:
        logging.warning(f"SYNC_ALERT_CHECK_TIME={check_time_str!r} 格式不对（应为 HH:MM），使用默认 09:00")
        hour, minute = 9, 0
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

# 邮件配置
SMTP_HOST = os.environ.get("SYNC_ALERT_SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SYNC_ALERT_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SYNC_ALERT_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SYNC_ALERT_SMTP_PASSWORD", "")
ALERT_TO = os.environ.get("SYNC_ALERT_TO", SMTP_USER)


def send_alert_email(subject: str, body: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        logging.error("未配置发件邮箱/授权码（.env 中 SYNC_ALERT_SMTP_USER / SYNC_ALERT_SMTP_PASSWORD），无法发送告警邮件")
        return False

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = ALERT_TO
    msg['Subject'] = Header(subject, 'utf-8')

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        logging.info(f"告警邮件已发送至 {ALERT_TO}")
        return True
    except Exception as e:
        logging.error(f"发送告警邮件失败: {e}")
        return False


def rclone_cat(remote_path: str):
    """读取远端文件内容，失败或不存在返回 None"""
    try:
        result = subprocess.run(
            ['rclone', 'cat', remote_path],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception as e:
        logging.debug(f"rclone cat 失败: {e}")
        return None


def cleanup_marker_files(remote_exists: bool):
    """清理本次检查产生的本地和云端临时标记文件"""
    if os.path.exists(LOCAL_MARKER):
        try:
            os.remove(LOCAL_MARKER)
            logging.info(f"已删除本地检测临时文件: {LOCAL_MARKER}")
        except Exception as e:
            logging.warning(f"删除本地检测临时文件失败: {e}")

    if remote_exists:
        try:
            result = subprocess.run(
                ['rclone', 'deletefile', REMOTE_MARKER],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                logging.info(f"已删除云端检测临时文件: {REMOTE_MARKER}")
            else:
                logging.warning(f"删除云端检测临时文件失败: {result.stderr}")
        except Exception as e:
            logging.warning(f"删除云端检测临时文件失败: {e}")


def check_sync_health() -> bool:
    if not os.path.exists(source_path):
        msg = f"同步源目录不存在: {source_path}"
        logging.error(msg)
        send_alert_email("【告警】我的网盘同步检查失败", msg)
        return False

    token = str(uuid.uuid4())
    content = f"healthcheck {datetime.now().isoformat()} {token}"

    try:
        with open(LOCAL_MARKER, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"已写入本地检测文件: {LOCAL_MARKER}")
    except Exception as e:
        msg = f"写入本地检测文件失败: {e}"
        logging.error(msg)
        send_alert_email("【告警】我的网盘同步检查失败", msg)
        return False

    start_time = time.time()
    synced = False

    try:
        while time.time() - start_time < CHECK_TIMEOUT_SECONDS:
            remote_content = rclone_cat(REMOTE_MARKER)
            if remote_content and token in remote_content:
                synced = True
                break
            logging.info("云端尚未检测到最新标记，等待下一次轮询...")
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        cleanup_marker_files(remote_exists=synced)

    elapsed = int(time.time() - start_time)

    if synced:
        logging.info(f"同步检测正常，耗时 {elapsed} 秒，云端已检测到最新文件")
        return True
    else:
        msg = (
            f"在 {CHECK_TIMEOUT_SECONDS // 60} 分钟内未检测到本地变更同步到云端。\n"
            f"本地目录: {source_path}\n"
            f"云端目标: {destination_path}\n"
            f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"请检查 Desktop-my_netdisk_sync_to_gdrive.py 监控服务是否正常运行，"
            f"以及 rclone 是否能正常访问 Google Drive。"
        )
        logging.error(f"同步检测失败: {msg}")
        send_alert_email("【告警】我的网盘同步异常", msg)
        return False


def run_forever():
    logging.info("===== 同步健康检查后台进程启动 =====")
    logging.info(f"每天检查时间: {CHECK_TIME_STR}")
    try:
        while True:
            next_run = next_daily_run_time(CHECK_TIME_STR)
            wait_seconds = (next_run - datetime.now()).total_seconds()
            logging.info(f"下次检查时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}，等待 {int(wait_seconds)} 秒")
            time.sleep(max(wait_seconds, 0))

            logging.info("----- 开始一次同步健康检查 -----")
            check_sync_health()
            logging.info("----- 本次检查结束 -----")
    except KeyboardInterrupt:
        logging.info("收到停止信号，健康检查进程退出")


def run_once():
    logging.info("===== 手动测试：跑一次同步健康检查 =====")
    ok = check_sync_health()
    logging.info(f"===== 本次检查结束，结果: {'正常' if ok else '异常'} =====\n")


def run_test_email():
    logging.info("===== 手动测试：发送测试邮件 =====")
    subject = "【测试】我的网盘同步告警邮件测试"
    body = (
        f"这是一封测试邮件，用于验证 Desktop-check_netdisk_sync_health.py 的邮件告警配置是否正确。\n"
        f"发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    ok = send_alert_email(subject, body)
    logging.info(f"===== 测试邮件发送{'成功' if ok else '失败'} =====\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="网盘同步健康检查")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="只跑一次检查，跑完立即退出（用于临时手动测试）")
    group.add_argument("--test-email", action="store_true", help="只测试邮件发送，不做同步检测")
    args = parser.parse_args()

    if args.test_email:
        run_test_email()
    elif args.once:
        run_once()
    else:
        run_forever()
