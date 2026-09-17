import subprocess
import os
import logging
import time
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


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


# 配置日志：固定写到脚本所在目录下的 my_netdisk_sync_to_gdrive.log，最多保留300行
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(BASE_DIR, "my_netdisk_sync_to_gdrive.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        LineLimitedFileHandler(log_file, max_lines=300),
        logging.StreamHandler()
    ]
)

# 源文件路径
source_path = r"D:\files\myNetdisk"

    
# 目标路径
destination_path = 'gdrive:/sync/Desktop/my_netdisk'

def dedupe_gdrive():
    """
    清理 Google Drive 上的重复文件
    """
    dedupe_command = [
        'rclone',
        'dedupe',
        '--dedupe-mode', 'newest',  # 保留最新的文件
        destination_path
    ]

    try:
        logging.info(f"开始清理重复文件: {destination_path}")
        process = subprocess.run(
            dedupe_command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        logging.info("重复文件清理完成")
        if process.stdout.strip():
            logging.info(f"清理结果: {process.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"清理重复文件失败: {e}")
        logging.error(f"错误输出: {e.stderr}")
        return False


def sync_to_gdrive():
    """
    使用rclone将我的网盘文件同步到Google Drive
    """
    # 检查源文件是否存在
    if not os.path.exists(source_path):
        logging.error(f"源文件不存在: {source_path}")
        return False

    # 构建rclone命令
    rclone_command = [
        'rclone',
        'sync',
        source_path,
        destination_path,
        '--progress',  # 显示进度
        '-v'  # 详细输出
    ]

    try:
        # 执行命令
        logging.info(f"开始同步: {source_path} -> {destination_path}")
        process = subprocess.run(
            rclone_command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',  # 明确指定 UTF-8 编码
            errors='replace',  # 遇到无法解码的字符时替换为 �
            creationflags=subprocess.CREATE_NO_WINDOW  # 添加这行来隐藏控制台窗口
        )

        # 记录输出
        logging.info("同步成功完成")
        logging.debug(f"命令输出: {process.stdout}")

        # 同步完成后清理重复文件
        dedupe_gdrive()

        return True

    except subprocess.CalledProcessError as e:
        # 处理错误
        logging.error(f"同步失败: {e}")
        logging.error(f"错误输出: {e.stderr}")
        return False

class FileHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_sync_time = 0
        self.sync_cooldown = 5  # 5秒冷却时间，防止目录变化时重复触发
        # 忽略的文件后缀（临时文件等）
        self.ignored_extensions = {'.tmp', '.temp', '.swp', '.~', '.crdownload', '.part'}

    def should_ignore(self, path):
        """检查是否应该忽略该文件"""
        # 忽略临时文件和特定后缀
        for ext in self.ignored_extensions:
            if path.endswith(ext):
                return True
        return False

    def trigger_sync(self, event_path):
        """触发同步操作"""
        current_time = time.time()
        if current_time - self.last_sync_time < self.sync_cooldown:
            return

        self.last_sync_time = current_time
        logging.info(f"检测到目录变化: {event_path}")
        start_time = datetime.now()
        result = sync_to_gdrive()
        end_time = datetime.now()
        duration = end_time - start_time

        if result:
            logging.info(f"同步任务完成，耗时: {duration}")
        else:
            logging.error(f"同步任务失败，耗时: {duration}")

    def on_modified(self, event):
        """文件或目录被修改"""
        if self.should_ignore(event.src_path):
            return
        logging.debug(f"文件修改: {event.src_path}")
        self.trigger_sync(event.src_path)

    def on_created(self, event):
        """文件或目录被创建"""
        if self.should_ignore(event.src_path):
            return
        logging.debug(f"文件创建: {event.src_path}")
        self.trigger_sync(event.src_path)

    def on_deleted(self, event):
        """文件或目录被删除"""
        if self.should_ignore(event.src_path):
            return
        logging.debug(f"文件删除: {event.src_path}")
        self.trigger_sync(event.src_path)

    def on_moved(self, event):
        """文件或目录被移动/重命名"""
        if self.should_ignore(event.src_path):
            return
        logging.debug(f"文件移动: {event.src_path} -> {event.dest_path}")
        self.trigger_sync(event.dest_path)

def watch_directory():
    """监控整个目录树的变化"""
    # 创建观察者
    observer = Observer()
    event_handler = FileHandler()

    # 递归监控整个目录树
    observer.schedule(event_handler, source_path, recursive=True)

    # 启动观察者
    observer.start()
    logging.info(f"开始监控目录: {source_path}")
    logging.info(f"递归监控: 是")
    logging.info(f"同步目标: {destination_path}")

    try:
        # 保持程序运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logging.info("监控停止")

    observer.join()

if __name__ == "__main__":
    logging.info(f"===== 启动自动同步服务 =====")
    logging.info(f"源目录: {source_path}")
    logging.info(f"目标路径: {destination_path}")

    # 先进行一次初始同步
    logging.info("执行初始同步...")
    sync_to_gdrive()

    # 开始监控目录变化
    logging.info("启动目录监控...")
    watch_directory()

    logging.info(f"===== 同步服务结束 =====\n")