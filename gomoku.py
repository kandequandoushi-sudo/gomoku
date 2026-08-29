# -*- coding: utf-8 -*-
"""
五子棋人机对战 —— Rapfi 引擎版
================================
使用 tkinter 绘制棋盘（15/19 路可选），通过 Gomocup/Piskvork 文本协议
与 Rapfi 五子棋引擎通信，实现人机对战。仅依赖 Python 标准库。

功能：
  - 玩家执黑 / 执白 / 随机先后手
  - 15 路 / 19 路棋盘切换
  - 自由规则 / 连珠禁手规则（黑棋三三、四四、长连禁手）
  - 四档难度（简单/中等/困难/极难，由引擎思考时间与搜索强度控制）
  - 悔棋 / 新游戏 / 认输
  - 最近一手落子高亮、着法记录、窗口缩放、全屏（F11 / ESC）
  - 内置 SSE2/AVX2/AVX-512/AVX-VNNI/AVX-512VNNI 五种引擎版本，
    启动时自动探测当前 CPU 可运行的最优版本

运行：python gomoku.py（需将 Rapfi 引擎置于 ./engine 目录，详见 README.md）
"""

import os
import sys
import time
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------------------- 常量 ----------------------------
BOARD_SIZE = 15          # 默认 15x15 棋盘
BOARD_SIZES = [15, 19]   # 可选棋盘路数
EMPTY, BLACK, WHITE = 0, 1, 2
ENGINE_NAME = "pbrain-rapfi-windows-avx2.exe"   # 回退默认值（探测失败时使用）

# 引擎可执行文件候选，按指令集从高到低排列；启动时自动探测当前 CPU 能运行的最优版本
ENGINE_CANDIDATES = [
    "pbrain-rapfi-windows-avx512vnni.exe",   # AVX-512 + VNNI
    "pbrain-rapfi-windows-avxvnni.exe",      # AVX-VNNI（Intel 11 代+）
    "pbrain-rapfi-windows-avx512.exe",       # AVX-512
    "pbrain-rapfi-windows-avx2.exe",         # AVX2（2013 年后主流）
    "pbrain-rapfi-windows-sse.exe",          # SSE2 基线（兼容老 CPU）
]

# 难度 -> 引擎每步思考时间(ms)
# 极难：15秒/步 + 多线程满载 + 512MB哈希表，接近比赛强度
DIFFICULTY = [
    ("简单", 200),
    ("中等", 800),
    ("困难", 2000),
    ("极难", 15000),
]

# 引擎强度参数（所有难度通用，思考时间由难度控制）
ENGINE_THREADS = max(1, (os.cpu_count() or 4))       # 用满全部 CPU 核心
ENGINE_HASH_KB = 524288                              # 置换表 512 MB（单位 KB）
ENGINE_MAX_NODE = 0                                  # 0 = 不限制节点数
ENGINE_MAX_DEPTH = 99                                # 最大搜索深度（0 会被引擎当成深度0，须用99表示不限）
ENGINE_CAUTION = 0                                   # 0 = 候选着法范围最广、最强

WIN_LEN = 5
DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))  # 横、竖、主对角、副对角

COLOR_NAME = {BLACK: "黑棋", WHITE: "白棋"}
COLOR_CN = {BLACK: "黑", WHITE: "白"}


# ------------------------- 工具函数 ---------------------------
def resource_dir():
    """引擎资源所在目录：
    优先使用「程序/脚本所在目录/engine」，其次使用 PyInstaller 的 _MEIPASS/engine。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        # onedir 模式 _MEIPASS 即程序目录；onefile 模式为临时解压目录
        cand = os.path.join(base, "engine")
        if os.path.isdir(cand):
            return cand
    cand = os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)), "engine")
    if os.path.isdir(cand):
        return cand
    if base:
        return os.path.join(base, "engine")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")


# ----------------------- 引擎版本自动探测 ----------------------
_DETECTED_ENGINE = {}

def detect_engine_path(engdir, timeout=4):
    """按指令集从高到低尝试启动引擎，返回当前 CPU 能运行的最优版本文件名。"""
    if engdir in _DETECTED_ENGINE:
        return _DETECTED_ENGINE[engdir]
    cf = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    for name in ENGINE_CANDIDATES:
        exe = os.path.join(engdir, name)
        if not os.path.isfile(exe):
            continue
        try:
            p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, cwd=engdir, bufsize=1,
                                 universal_newlines=True, encoding="utf-8",
                                 errors="replace", creationflags=cf)
        except Exception:
            continue
        ok = False
        try:
            p.stdin.write("START 15\n"); p.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                if p.poll() is not None:
                    break
                line = p.stdout.readline()
                if line and line.strip() == "OK":
                    ok = True; break
        except Exception:
            pass
        finally:
            try:
                if p.poll() is None:
                    p.stdin.write("END\n"); p.stdin.flush(); p.wait(timeout=2)
            except Exception:
                try: p.kill()
                except Exception: pass
        if ok:
            _DETECTED_ENGINE[engdir] = name
            LOG("引擎探测: 选择 %s" % name)
            return name
        LOG("引擎探测: %s 不可用" % name)
    _DETECTED_ENGINE[engdir] = ENGINE_NAME
    return ENGINE_NAME


# ------------------------- 调试日志 ---------------------------
LOG_FILE = None


def setup_log():
    global LOG_FILE
    try:
        base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
        LOG_FILE = os.path.join(base, "gomoku.log")
    except Exception:
        LOG_FILE = None


def LOG(msg):
    if not LOG_FILE:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# ------------------------ 引擎通信层 --------------------------
class RapfiEngine:
    """Rapfi 引擎进程封装（Gomocup/Piskvork 协议）。
    采用「专用读取线程 + 行队列」架构，避免管道读取竞争与阻塞。"""

    def __init__(self, thinking_ms=800, board_size=15):
        self.thinking_ms = thinking_ms
        self.board_size = board_size
        self.rule = 0              # 0=自由, 2=禁手(连珠规则)
        self.proc = None
        self._lineq = queue.Queue()
        self._reader = None
        self._alive = False
        self.engdir = resource_dir()
        self.engfile = detect_engine_path(self.engdir)   # 自动选择当前 CPU 最优版本

    # ---------- 进程管理 ----------
    def start(self, board_size=None):
        self.stop()
        if board_size is not None:
            self.board_size = board_size
        # CREATE_NO_WINDOW：禁止引擎弹出控制台黑窗口
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # 按优先级尝试启动引擎（self.engfile 是探测出的最优版本，失败时依次回退）
        tried = []
        popen_err = None
        for name in [self.engfile] + [n for n in ENGINE_CANDIDATES if n != self.engfile]:
            exe = os.path.join(self.engdir, name)
            if not os.path.isfile(exe) or name in tried:
                continue
            tried.append(name)
            try:
                LOG("启动引擎: %s (cwd=%s)" % (exe, self.engdir))
                self.proc = subprocess.Popen(
                    [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, cwd=self.engdir, bufsize=1,
                    universal_newlines=True, encoding="utf-8",
                    errors="replace", creationflags=creationflags,
                )
                self.engfile = name
                break
            except OSError as e:
                popen_err = e
                LOG("引擎 %s 启动失败: %s" % (name, e))
                self.proc = None
                continue
        if self.proc is None:
            msg = "无法启动引擎（已尝试 %s）。" % "、".join(tried)
            if popen_err is not None and getattr(popen_err, "winerror", 0) == 4551:
                msg += "\nWindows 智能应用控制(Smart App Control)或杀毒软件拦截了引擎程序。"
                msg += "\n请在 Windows 安全中心 → 应用和浏览器控制 中关闭智能应用控制，"
                msg += "\n或将程序目录添加到杀毒软件白名单后重试。"
            else:
                msg += "\n可能被安全软件拦截或引擎文件损坏，请检查后重试。"
            LOG("FATAL %s" % msg)
            raise RuntimeError(msg)
        self._alive = True
        # 每次启动重建全新行队列，避免上一次引擎的 EOF 哨兵残留污染本次响应
        self._lineq = queue.Queue()
        lineq = self._lineq
        self._reader = threading.Thread(target=self._read_loop, args=(lineq,), daemon=True)
        self._reader.start()

        # 建立指定路数棋盘（等待引擎加载完成并回复 OK）
        resp = self._send("START %d" % self.board_size, timeout=40)
        LOG("START 响应: %r" % (resp,))
        if resp is None:
            LOG("FATAL START 未获响应，引擎可能未启动")
            raise RuntimeError("引擎未响应 START，可能被系统安全软件拦截或引擎文件异常。")
        # 设置思考时间（INFO 引擎不回复，fire-and-forget）
        self.set_timeout(self.thinking_ms, wait=False)
        # 设置规则（0=自由, 2=禁手）
        self._send_info_rule()
        # 强度参数：多线程、大哈希表、不限节点/深度、最广候选范围
        self._send_strength_settings()

    def _send_strength_settings(self):
        """向引擎发送强度相关 INFO 参数（线程数/哈希表/节点深度限制等）。"""
        if not self._alive:
            return
        cmds = [
            "INFO thread_num %d" % ENGINE_THREADS,
            "INFO hash_size %d" % ENGINE_HASH_KB,
            "INFO max_node %d" % ENGINE_MAX_NODE,
            "INFO max_depth %d" % ENGINE_MAX_DEPTH,
            "INFO caution_factor %d" % ENGINE_CAUTION,
        ]
        try:
            for cmd in cmds:
                self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            LOG("引擎强度参数: threads=%d hash=%dKB max_node=%d max_depth=%d caution=%d"
                % (ENGINE_THREADS, ENGINE_HASH_KB, ENGINE_MAX_NODE, ENGINE_MAX_DEPTH, ENGINE_CAUTION))
        except Exception:
            pass

    def _send_info_rule(self):
        """向引擎发送当前规则设置（INFO rule）。"""
        if not self._alive:
            return
        try:
            self.proc.stdin.write("INFO rule %d\n" % self.rule)
            self.proc.stdin.flush()
        except Exception:
            pass

    def set_rule(self, forbidden):
        """切换禁手规则：forbidden=True 启用黑棋禁手(三三/四四/长连)，False 自由规则。"""
        self.rule = 2 if forbidden else 0
        LOG("引擎规则切换: rule=%d (%s)" % (self.rule, "禁手" if forbidden else "自由"))
        self._send_info_rule()

    def stop(self):
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    try:
                        self.proc.stdin.write("END\n")
                        self.proc.stdin.flush()
                    except Exception:
                        pass
                    try:
                        self.proc.wait(timeout=3)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
        self._alive = False

    def reset(self, thinking_ms=None, board_size=None):
        self.stop()
        if thinking_ms is not None:
            self.thinking_ms = thinking_ms
        if board_size is not None:
            self.board_size = board_size
        self.start()

    # ---------- 读取 ----------
    def _read_loop(self, lineq):
        """后台线程：持续把引擎输出行放入队列。lineq 为本线程专属队列。"""
        try:
            while self._alive:
                line = self.proc.stdout.readline()
                if line == "":
                    break
                lineq.put(line.rstrip("\r\n"))
        except Exception:
            pass
        try:
            lineq.put(None)   # EOF 哨兵
        except Exception:
            pass
        LOG("引擎输出流已结束(EOF)")

    def _read_reply(self, timeout):
        """从队列读一条响应：跳过 MESSAGE 行，返回第一条非 MESSAGE 行。
        超时或 EOF 返回 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._lineq.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                return None
            if line is None:      # EOF
                return None
            if not line:
                continue
            if line.startswith("MESSAGE"):
                continue
            return line
        return None

    def _send(self, cmd, timeout=30):
        """发送单行/多行命令并读取一条响应。"""
        if not self._alive or self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("引擎未运行")
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            LOG("发送命令异常: %r -> %s" % (cmd[:60], e))
            raise RuntimeError("向引擎发送命令失败：%s" % e)
        resp = self._read_reply(timeout)
        LOG("CMD %s -> %r" % (cmd.split("\n")[0][:40], resp))
        return resp

    # ---------- 高层操作 ----------
    def set_timeout(self, ms, wait=True):
        """设置每步思考时间。INFO 命令引擎不回复：默认不等待。"""
        self.thinking_ms = int(ms)
        if not self._alive:
            return
        try:
            self.proc.stdin.write("INFO timeout_turn %d\n" % self.thinking_ms)
            self.proc.stdin.flush()
        except Exception:
            pass
        if wait:
            # 冲刷可能存在的响应（短超时）
            self._read_reply(0.3)

    def send_board(self, board, board_size=None, timeout=120):
        """把整个棋盘发给引擎，让引擎在当前局面下走一步。
        返回引擎落子 (col, row)；引擎无合法走法返回 None。"""
        size = board_size if board_size is not None else self.board_size
        # 每次摆局前重新下发思考时间与强度参数，避免 BOARD/DONE 后被引擎重置
        try:
            self.proc.stdin.write("INFO timeout_turn %d\n" % self.thinking_ms)
            self.proc.stdin.write("INFO timeout_match %d\n" % int(self.thinking_ms * 1000 + 500))
            self.proc.stdin.write("INFO thread_num %d\n" % ENGINE_THREADS)
            self.proc.stdin.write("INFO hash_size %d\n" % ENGINE_HASH_KB)
            self.proc.stdin.write("INFO max_node %d\n" % ENGINE_MAX_NODE)
            self.proc.stdin.write("INFO max_depth %d\n" % ENGINE_MAX_DEPTH)
            self.proc.stdin.write("INFO caution_factor %d\n" % ENGINE_CAUTION)
            self.proc.stdin.flush()
        except Exception:
            pass
        lines = ["BOARD"]
        for row in range(size):
            for col in range(size):
                if board[row][col] != EMPTY:
                    lines.append("%d,%d,%d" % (col, row, board[row][col]))
        lines.append("DONE")
        resp = self._send("\n".join(lines), timeout=timeout)
        if resp is None:
            return None
        if resp.upper().startswith("ERROR"):
            return None
        if resp.upper() == "UNKNOWN":
            return None
        try:
            x, y = resp.strip().split(",")
            return int(x), int(y)
        except Exception:
            return None


# ------------------------- 胜负判定 ---------------------------
def check_win(board, color, board_size=BOARD_SIZE, black_forbidden=False):
    """返回 True 表示 color 已获胜。
    black_forbidden=True 时黑棋必须正好五连才算赢（长连不算）；白棋仍 >=5 即赢。"""
    for row in range(board_size):
        for col in range(board_size):
            if board[row][col] != color:
                continue
            for dx, dy in DIRECTIONS:
                # 只从连续同色段的起点开始数，避免长连被截成5
                pr, pc = row - dy, col - dx
                if 0 <= pr < board_size and 0 <= pc < board_size and board[pr][pc] == color:
                    continue
                cnt = 1
                r, c = row + dy, col + dx
                while 0 <= r < board_size and 0 <= c < board_size and board[r][c] == color:
                    cnt += 1
                    r += dy
                    c += dx
                if black_forbidden and color == BLACK:
                    if cnt == WIN_LEN:
                        return True
                else:
                    if cnt >= WIN_LEN:
                        return True
    return False


def is_board_full(board, board_size=BOARD_SIZE):
    return all(board[r][c] != EMPTY for r in range(board_size) for c in range(board_size))


def star_points(board_size):
    """返回指定路数棋盘的星位 (row, col) 列表。"""
    if board_size == 15:
        return [(3, 3), (3, 11), (7, 7), (11, 3), (11, 11)]
    if board_size == 19:
        return [(3, 3), (3, 9), (3, 15), (9, 3), (9, 9), (9, 15), (15, 3), (15, 9), (15, 15)]
    return [(board_size // 2, board_size // 2)]


# ------------------------- 禁手判定 ---------------------------
def _line_count(board, row, col, dr, dc, color, size):
    """(row,col) 沿 (dr,dc) 方向连续 color 的棋子数（含自身）。"""
    cnt = 1
    r, c = row + dr, col + dc
    while 0 <= r < size and 0 <= c < size and board[r][c] == color:
        cnt += 1; r += dr; c += dc
    r, c = row - dr, col - dc
    while 0 <= r < size and 0 <= c < size and board[r][c] == color:
        cnt += 1; r -= dr; c -= dc
    return cnt


def _segment_bounds(board, row, col, dr, dc, color, size):
    """返回包含 (row,col) 的连续 color 段的 (前r,前c,后r,后c,长度)。"""
    r1, c1 = row, col
    while 0 <= r1 - dr < size and 0 <= c1 - dc < size and board[r1 - dr][c1 - dc] == color:
        r1 -= dr; c1 -= dc
    r2, c2 = row, col
    while 0 <= r2 + dr < size and 0 <= c2 + dc < size and board[r2 + dr][c2 + dc] == color:
        r2 += dr; c2 += dc
    length = (abs(r2 - r1) + abs(c2 - c1)) + 1
    return r1, c1, r2, c2, length


def _forms_four(board, row, col, dr, dc, color, size):
    """落子后，该方向是否形成「四」：存在一个空位，填 color 后形成正好五连。"""
    for k in range(-4, 5):
        if k == 0:
            continue
        r, c = row + dr * k, col + dc * k
        if not (0 <= r < size and 0 <= c < size) or board[r][c] != EMPTY:
            continue
        board[r][c] = color
        cnt = _line_count(board, row, col, dr, dc, color, size)
        board[r][c] = EMPTY
        if cnt == 5:
            return True
    return False


def _forms_open_three(board, row, col, dr, dc, color, size):
    """落子后，该方向是否形成「活三」：存在一个空位，填 color 后形成活四（四连两端皆空）。"""
    for k in range(-4, 5):
        if k == 0:
            continue
        r, c = row + dr * k, col + dc * k
        if not (0 <= r < size and 0 <= c < size) or board[r][c] != EMPTY:
            continue
        board[r][c] = color
        r1, c1, r2, c2, length = _segment_bounds(board, row, col, dr, dc, color, size)
        board[r][c] = EMPTY
        if length != 4:
            continue
        br, bc = r1 - dr, c1 - dc
        ar, ac = r2 + dr, c2 + dc
        before_open = 0 <= br < size and 0 <= bc < size and board[br][bc] == EMPTY
        after_open = 0 <= ar < size and 0 <= ac < size and board[ar][ac] == EMPTY
        if before_open and after_open:
            return True
    return False


def is_forbidden_move(board, row, col, color, size):
    """判断黑棋在 (row,col) 落子（board[row][col] 须已置为 color）是否构成禁手。
    返回 (forbidden, reason)。白棋无禁手。五连优先（成五不禁）。"""
    if color != BLACK:
        return False, None
    # 1) 五连优先：任一方向正好五连即获胜，不禁
    for dr, dc in DIRECTIONS:
        if _line_count(board, row, col, dr, dc, color, size) == 5:
            return False, None
    # 2) 长连禁手：任一方向连续 6 子及以上
    for dr, dc in DIRECTIONS:
        if _line_count(board, row, col, dr, dc, color, size) >= 6:
            return True, "长连禁手"
    # 3) 四四禁手
    four_dirs = set()
    for i, (dr, dc) in enumerate(DIRECTIONS):
        if _forms_four(board, row, col, dr, dc, color, size):
            four_dirs.add(i)
    if len(four_dirs) >= 2:
        return True, "四四禁手"
    # 4) 三三禁手（已是「四」的方向不再重复计为活三）
    three_count = 0
    for i, (dr, dc) in enumerate(DIRECTIONS):
        if i in four_dirs:
            continue
        if _forms_open_three(board, row, col, dr, dc, color, size):
            three_count += 1
    if three_count >= 2:
        return True, "三三禁手"
    return False, None


# --------------------------- 界面 ------------------------------
class GomokuApp:
    def __init__(self, root):
        self.root = root
        self.root.title("五子棋 · Rapfi 人机对战")
        self.root.resizable(True, True)
        self.root.minsize(820, 680)

        # 棋盘数据
        self.board_size = BOARD_SIZE
        self.board = [[EMPTY] * self.board_size for _ in range(self.board_size)]
        self.history = []          # [(color, col, row), ...]
        self.turn = BLACK          # 当前该谁走
        self.player_color = BLACK  # 玩家执子
        self.ai_color = WHITE
        self.ai_thinking = False
        self.game_over = False
        self.forbidden_mode = False   # 禁手规则（仅约束黑棋：三三/四四/长连）

        # 引擎
        self.engine = RapfiEngine(thinking_ms=DIFFICULTY[1][1])
        self.cmd_queue = queue.Queue()   # 引擎任务
        self.engine_thread = None

        # 绘制参数（初始值；窗口/棋盘尺寸变化时由 _update_layout 动态重算）
        self.canvas_size = 620
        self.board_px = 620
        self.margin = 30
        self.cell = (self.canvas_size - 2 * self.margin) // (self.board_size - 1)
        self.piece_r = self.cell * 0.42
        self.offset_x = 0
        self.offset_y = 0
        self._resize_after_id = None
        self.fullscreen = False

        self._build_ui()
        self._start_engine_thread()
        self.root.after(100, self._on_startup)

    # ---------- UI 构建 ----------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # 左侧棋盘（随窗口大小自适应）
        self.canvas = tk.Canvas(
            main, width=self.canvas_size, height=self.canvas_size,
            bg="#e8c876", highlightthickness=2, highlightbackground="#8b5a2b",
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._draw_board()

        # 右侧控制区
        right = ttk.Frame(main, padding=(16, 4))
        right.pack(side="left", fill="y")

        ttk.Label(right, text="五子棋 · Rapfi", font=("Microsoft YaHei", 18, "bold")).pack(anchor="w", pady=(0, 2))
        ttk.Label(right, text="Rapfi 引擎 人机对战", foreground="#666").pack(anchor="w", pady=(0, 14))

        ttk.Label(right, text="对局状态", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="准备中...")
        ttk.Label(right, textvariable=self.status_var, font=("Microsoft YaHei", 12, "bold"),
                  foreground="#b8860b", wraplength=200).pack(anchor="w", pady=(4, 12))

        ttk.Label(right, text="难度（AI 思考时间）", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        self.diff_var = tk.StringVar(value=DIFFICULTY[1][0])
        self.diff_box = ttk.Combobox(
            right, textvariable=self.diff_var, state="readonly", width=16,
            values=[d[0] for d in DIFFICULTY])
        self.diff_box.pack(anchor="w", pady=(4, 12))
        self.diff_box.bind("<<ComboboxSelected>>", self._on_diff_change)

        ttk.Label(right, text="棋盘路数", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        self.size_var = tk.StringVar(value="%d 路" % self.board_size)
        self.size_box = ttk.Combobox(
            right, textvariable=self.size_var, state="readonly", width=16,
            values=["%d 路" % s for s in BOARD_SIZES])
        self.size_box.pack(anchor="w", pady=(4, 12))
        self.size_box.bind("<<ComboboxSelected>>", self._on_size_change)

        ttk.Label(right, text="执子选择", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        self.color_var = tk.StringVar(value="黑棋先手")
        color_frame = ttk.Frame(right)
        color_frame.pack(anchor="w", pady=(4, 4))
        for text in ("黑棋先手", "白棋后手", "随机"):
            ttk.Radiobutton(color_frame, text=text, value=text,
                            variable=self.color_var).pack(anchor="w")
        ttk.Label(right, text="（执黑先手适合新手；执白由 AI 先落子）",
                  foreground="#999", font=("Microsoft YaHei", 8)).pack(anchor="w", pady=(0, 8))

        self.forbidden_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="禁手规则（黑棋三三/四四/长连禁手）",
                        variable=self.forbidden_var,
                        command=self._on_forbidden_change).pack(anchor="w", pady=(0, 8))

        btn_frame = ttk.Frame(right)
        btn_frame.pack(anchor="w", pady=(4, 4))
        ttk.Button(btn_frame, text="新游戏", command=self._new_game).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btn_frame, text="悔棋", command=self._undo).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(btn_frame, text="认输", command=self._resign).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(btn_frame, text="全屏", command=self._toggle_fullscreen).grid(row=0, column=3)

        self.hint_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.hint_var, foreground="#888",
                  font=("Microsoft YaHei", 8), wraplength=210, justify="left").pack(anchor="w", pady=(14, 0))

        ttk.Label(right, text="着法记录", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(12, 4))
        self.move_list = tk.Listbox(right, width=22, height=10, font=("Consolas", 9))
        self.move_list.pack(anchor="w", fill="both", expand=True)

        ttk.Label(right, text="F11 切换全屏 · ESC 退出全屏",
                  foreground="#999", font=("Microsoft YaHei", 8)).pack(anchor="w", pady=(8, 0))

        # 全屏快捷键
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)

    # ---------- 尺寸自适应 ----------
    def _update_layout(self):
        """根据 canvas 当前实际尺寸重算棋盘几何参数，棋盘始终居中、保持正方形。"""
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            return False
        board_px = min(w, h)
        margin = max(18, int(board_px * 0.045))
        self.board_px = board_px
        self.margin = margin
        self.cell = (board_px - 2 * margin) / (self.board_size - 1)
        self.piece_r = self.cell * 0.42
        self.offset_x = (w - board_px) // 2
        self.offset_y = (h - board_px) // 2
        return True

    def _on_canvas_configure(self, event):
        # 防抖：拖动窗口尺寸时避免频繁重绘
        if self._resize_after_id:
            self.root.after_cancel(self._resize_after_id)
        self._resize_after_id = self.root.after(80, self._do_redraw)

    def _do_redraw(self):
        self._resize_after_id = None
        if self._update_layout():
            self._draw_board()

    # ---------- 全屏 ----------
    def _toggle_fullscreen(self, _=None):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)
        if self.fullscreen:
            self.hint_var.set("已进入全屏，按 F11 或 ESC 退出。")
        else:
            self.hint_var.set("已退出全屏。")

    def _exit_fullscreen(self, _=None):
        if self.fullscreen:
            self._toggle_fullscreen()

    def _draw_board(self):
        if not self._update_layout():
            return
        c = self.canvas
        c.delete("all")
        ox, oy, m = self.offset_x, self.offset_y, self.margin
        n = self.board_size
        span = self.cell * (n - 1)
        for i in range(n):
            x = ox + m + i * self.cell
            c.create_line(x, oy + m, x, oy + m + span, fill="#5a3d1a", width=1)
            y = oy + m + i * self.cell
            c.create_line(ox + m, y, ox + m + span, y, fill="#5a3d1a", width=1)
        dot_r = max(3, self.cell * 0.09)
        for (r, cc) in star_points(n):
            px = ox + m + cc * self.cell
            py = oy + m + r * self.cell
            c.create_oval(px - dot_r, py - dot_r, px + dot_r, py + dot_r, fill="#5a3d1a", outline="")
        for r in range(n):
            for cc in range(n):
                if self.board[r][cc] != EMPTY:
                    self._draw_piece(cc, r, self.board[r][cc])

    def _draw_piece(self, col, row, color):
        px = self.offset_x + self.margin + col * self.cell
        py = self.offset_y + self.margin + row * self.cell
        if color == BLACK:
            self.canvas.create_oval(px - self.piece_r, py - self.piece_r,
                                    px + self.piece_r, py + self.piece_r,
                                    fill="#111", outline="#000", width=1)
            self.canvas.create_oval(px - self.piece_r * 0.45, py - self.piece_r * 0.5,
                                    px - self.piece_r * 0.05, py - self.piece_r * 0.15,
                                    fill="#555", outline="")
        else:
            self.canvas.create_oval(px - self.piece_r, py - self.piece_r,
                                    px + self.piece_r, py + self.piece_r,
                                    fill="#f5f5f5", outline="#888", width=1)
        if self.history and self.history[-1][1] == col and self.history[-1][2] == row:
            # 最近一手高亮：亮红色实心圆点，黑白子上都醒目
            mark_r = max(3, self.cell * 0.11)
            self.canvas.create_oval(px - mark_r, py - mark_r, px + mark_r, py + mark_r,
                                    fill="#ff1744", outline="#fff", width=1)

    # ---------- 引擎线程 ----------
    def _start_engine_thread(self):
        self.engine_thread = threading.Thread(target=self._engine_loop, daemon=True)
        self.engine_thread.start()

    def _engine_loop(self):
        while True:
            task = self.cmd_queue.get()
            if task is None:
                break
            kind, payload, res_q = task
            try:
                if kind == "send_board":
                    board, bsize = payload
                    move = self.engine.send_board(board, board_size=bsize)
                    res_q.put(("move", move))
                elif kind == "set_timeout":
                    self.engine.set_timeout(payload)
                    res_q.put(("ok", None))
                elif kind == "set_rule":
                    self.engine.set_rule(payload)
                    res_q.put(("ok", None))
            except Exception as e:
                res_q.put(("error", str(e)))

    def _request_engine(self, kind, payload=None):
        q = queue.Queue()
        self.cmd_queue.put((kind, payload, q))
        return q

    def _poll_result(self, res_q, on_ok, on_err):
        def poll():
            try:
                status, data = res_q.get_nowait()
            except queue.Empty:
                self.root.after(60, poll)
                return
            if status == "move":
                on_ok(data)
            elif status == "ok":
                on_ok(None)
            elif status == "error":
                on_err(data)
        self.root.after(60, poll)

    # ---------- 对局流程 ----------
    def _on_startup(self):
        try:
            self.engine.start()
        except Exception as e:
            self.status_var.set("引擎启动失败")
            messagebox.showerror("引擎错误",
                                 "Rapfi 引擎启动失败：\n%s\n\n请确认程序所在目录的 engine 文件夹完整。" % e)
            return
        self._new_game(first=True)

    def _new_game(self, first=False):
        if not first:
            choice = self.color_var.get()
            if choice == "白棋后手":
                pc = WHITE
            elif choice == "随机":
                pc = BLACK if int(time.time() * 1000) % 2 == 0 else WHITE
            else:
                pc = BLACK
            self.board_size = self._current_size()
        else:
            pc = BLACK
        self.player_color = pc
        self.ai_color = WHITE if pc == BLACK else BLACK
        self.forbidden_mode = bool(self.forbidden_var.get())

        self.board = [[EMPTY] * self.board_size for _ in range(self.board_size)]
        self.history = []
        self.move_list.delete(0, "end")
        self.game_over = False
        self.ai_thinking = False
        self.turn = BLACK
        self.hint_var.set("提示：点击交叉点落子。支持悔棋、切换难度、换执子。")

        try:
            ms = self._current_diff_ms()
            # 先设置引擎规则，reset 启动时会随 INFO rule 一起下发
            self.engine.rule = 2 if self.forbidden_mode else 0
            self.engine.reset(thinking_ms=ms, board_size=self.board_size)
        except Exception as e:
            self.status_var.set("引擎重启失败：%s" % e)
            return

        self._draw_board()
        if self.player_color == WHITE:
            self.status_var.set("AI（黑棋）思考中...")
            self.ai_thinking = True
            res_q = self._request_engine("send_board", ([row[:] for row in self.board], self.board_size))
            self._poll_result(res_q, self._on_ai_move, self._on_engine_error)
        else:
            self.status_var.set("轮到你走（黑棋）")

    def _current_size(self):
        try:
            return int(self.size_var.get().split("路")[0])
        except Exception:
            return self.board_size

    def _on_size_change(self, _=None):
        new_size = self._current_size()
        if new_size == self.board_size and not self.history:
            return
        if self.ai_thinking:
            self.hint_var.set("AI 思考中，请稍候再切换棋盘路数。")
            self.size_var.set("%d 路" % self.board_size)
            return
        # 切换路数需要重启引擎并开新局
        if self.history and not self.game_over:
            if not messagebox.askyesno("切换棋盘", "切换棋盘路数将开始新游戏，当前对局会丢失，是否继续？"):
                self.size_var.set("%d 路" % self.board_size)
                return
        self._new_game()

    def _on_forbidden_change(self):
        if self.ai_thinking:
            self.forbidden_var.set(self.forbidden_mode)
            self.hint_var.set("AI 思考中，请稍候再切换规则。")
            return
        want = bool(self.forbidden_var.get())
        if want == self.forbidden_mode:
            return
        # 对局中切换规则需要开新局
        if self.history and not self.game_over:
            if not messagebox.askyesno("切换规则", "切换禁手规则将开始新游戏，当前对局会丢失，是否继续？"):
                self.forbidden_var.set(self.forbidden_mode)
                return
        self._new_game()

    def _current_diff_ms(self):
        name = self.diff_var.get()
        for n, ms in DIFFICULTY:
            if n == name:
                return ms
        return 800

    def _on_diff_change(self, _=None):
        if self.ai_thinking or self.game_over:
            return
        try:
            ms = self._current_diff_ms()
            res_q = self._request_engine("set_timeout", ms)
            sec = ms / 1000.0
            time_text = "%g秒" % sec if sec < 10 else "%d秒" % int(sec)
            self.hint_var.set("难度已调整为“%s”（每步思考约 %s，%d线程满载）"
                              % (self.diff_var.get(), time_text, ENGINE_THREADS))
        except Exception:
            pass

    # ---------- 玩家落子 ----------
    def _on_click(self, event):
        if self.game_over or self.ai_thinking:
            return
        if self.turn != self.player_color:
            self.status_var.set("请等待 AI 落子...")
            return
        col = round((event.x - self.offset_x - self.margin) / self.cell)
        row = round((event.y - self.offset_y - self.margin) / self.cell)
        if not (0 <= col < self.board_size and 0 <= row < self.board_size):
            return
        if self.board[row][col] != EMPTY:
            self.hint_var.set("此处已有棋子，请选择空位。")
            return
        # 禁手检查：禁手模式下黑棋不能在三三/四四/长连禁手点落子
        if self.forbidden_mode and self.player_color == BLACK:
            self.board[row][col] = BLACK
            forbidden, reason = is_forbidden_move(self.board, row, col, BLACK, self.board_size)
            self.board[row][col] = EMPTY
            if forbidden:
                self.hint_var.set("此处为禁手点（%s），黑棋不能在此落子。" % reason)
                self.status_var.set("禁手：%s" % reason)
                return
        self._place(row, col, self.player_color)

    def _place(self, row, col, color):
        self.board[row][col] = color
        self.history.append((color, col, row))
        self._draw_board()   # 重绘整盘，确保「最近一手」红点只留在最新落子上
        self.move_list.insert("end", "%02d. %s %s,%s" % (len(self.history), COLOR_CN[color], col, row))
        self.move_list.see("end")

        if check_win(self.board, color, self.board_size, black_forbidden=self.forbidden_mode):
            self.game_over = True
            self.status_var.set("恭喜，你赢了！" if color == self.player_color else "AI 获胜！")
            messagebox.showinfo("游戏结束", "你赢了！" if color == self.player_color else "AI 获胜，再接再厉！")
            return
        if is_board_full(self.board, self.board_size):
            self.game_over = True
            self.status_var.set("平局")
            messagebox.showinfo("游戏结束", "棋盘已满，平局。")
            return

        # 只有「玩家落子」后才轮到 AI；AI 落子后交回玩家，避免 AI 连下
        if color == self.player_color:
            self.turn = self.ai_color
            self.status_var.set("AI（%s）思考中..." % COLOR_NAME[self.ai_color])
            self.ai_thinking = True
            res_q = self._request_engine("send_board", ([row[:] for row in self.board], self.board_size))
            self._poll_result(res_q, self._on_ai_move, self._on_engine_error)
        else:
            self.turn = self.player_color
            self.status_var.set("轮到你走（%s）" % COLOR_NAME[self.player_color])

    # ---------- AI 落子 ----------
    def _on_ai_move(self, move):
        self.ai_thinking = False
        if self.game_over:
            return
        if move is None:
            self.status_var.set("引擎未返回走法")
            return
        col, row = move
        if not (0 <= col < self.board_size and 0 <= row < self.board_size):
            self.status_var.set("引擎返回非法坐标：%s" % (move,))
            return
        if self.board[row][col] != EMPTY:
            self.status_var.set("引擎落子冲突：%s" % (move,))
            return
        # 防御：禁手模式下 AI 执黑若返回禁手点，判 AI 负（引擎 rule=2 正常不会触发）
        if self.forbidden_mode and self.ai_color == BLACK:
            self.board[row][col] = BLACK
            forbidden, reason = is_forbidden_move(self.board, row, col, BLACK, self.board_size)
            self.board[row][col] = EMPTY
            if forbidden:
                self.board[row][col] = BLACK
                self.history.append((BLACK, col, row))
                self._draw_board()
                self.game_over = True
                msg = "AI 落入禁手（%s），你赢了！" % reason
                self.status_var.set(msg)
                messagebox.showinfo("游戏结束", msg)
                return
        self._place(row, col, self.ai_color)
        if self.game_over:
            return
        self.turn = self.player_color
        self.status_var.set("轮到你走（%s）" % COLOR_NAME[self.player_color])

    def _on_engine_error(self, err):
        self.ai_thinking = False
        self.status_var.set("引擎错误")
        self.hint_var.set("引擎错误：%s\n可点击“新游戏”重启引擎。" % err)

    # ---------- 操作 ----------
    def _undo(self):
        if self.ai_thinking:
            return
        if self.game_over:
            return
        if not self.history:
            self.hint_var.set("还没有可悔的棋。")
            return
        removed = 0
        if self.history and self.history[-1][0] == self.ai_color:
            self._pop_last()
            removed += 1
        if self.history and self.history[-1][0] == self.player_color:
            self._pop_last()
            removed += 1
        self._draw_board()

        self.turn = self._next_turn()
        if self.turn == self.ai_color:
            # 悔棋后轮到 AI：可能是 AI 重新开局（空盘），也可能是 AI 补走
            if not self.history:
                self.hint_var.set("已悔棋 %d 步，AI 将重新开局。" % removed)
            else:
                self.hint_var.set("已悔棋 %d 步。" % removed)
            self.status_var.set("AI（%s）思考中..." % COLOR_NAME[self.ai_color])
            self.ai_thinking = True
            res_q = self._request_engine("send_board", ([row[:] for row in self.board], self.board_size))
            self._poll_result(res_q, self._on_ai_move, self._on_engine_error)
        else:
            self.hint_var.set("已悔棋 %d 步。" % removed)
            self.status_var.set("轮到你走（%s）" % COLOR_NAME[self.player_color])

    def _next_turn(self):
        if not self.history:
            return BLACK
        return WHITE if self.history[-1][0] == BLACK else BLACK

    def _pop_last(self):
        if not self.history:
            return
        color, col, row = self.history.pop()
        self.board[row][col] = EMPTY
        self.move_list.delete("end")

    def _resign(self):
        if self.game_over or self.ai_thinking:
            return
        self.game_over = True
        self.status_var.set("你认输了，AI 获胜。")
        messagebox.showinfo("认输", "你认输了，AI 获胜。")

    def on_close(self):
        try:
            self.cmd_queue.put(None)
        except Exception:
            pass
        self.engine.stop()
        self.root.destroy()


# ---------------------------- 入口 ----------------------------
def main():
    setup_log()
    LOG("==== 程序启动 ====")
    root = tk.Tk()
    app = GomokuApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
