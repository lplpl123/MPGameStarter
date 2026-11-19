# -*- coding: utf-8 -*-
"""
游戏登记管理器（仅登记游戏可执行程序）
--------------------------------------------------
功能:
  1. 登记本机游戏程序（exe / bat / cmd / lnk / app 等）
  2. 双击列表即可启动游戏
  3. 右键菜单：启动、打开所在文件夹、编辑、删除
  4. 数据自动保存到 JSON，下次打开自动加载
  5. 关闭窗口时最小化到系统托盘，双击托盘图标恢复
  6. 图标资源兼容 PyInstaller 打包环境，任务栏图标分组正确
  7. ★ 改动：主界面改为 Switch 风格横向卡片架，所有游戏始终位于窗口垂直中间；
     「排序」菜单支持按名称 / 时长排序或恢复登记顺序
  8. ★ 新增：主界面支持自定义背景图片，可调整背景不透明度并自动记住设置
  9. ★ 新增：「同步」按钮可从云端（飞书多维表格）拉取游戏时长并合并到本地
  10. ★ 新增：「上传」按钮可把本地最新时长写入云端表格（已有记录更新，缺失记录新增）
  11. ★ 改动：playtime.json 移至用户主目录（~/.game_manager_playtime.json），
      与设置文件同位置

依赖:
  pip install PyQt5 lark-oapi psutil
"""
import sys
import shlex
import uuid
import subprocess
import threading
import ctypes

#   确保 Windows 任务栏和系统托盘图标按本程序分组，而不是显示 Python 图标
if sys.platform == "win32":
    _APP_USER_MODEL_ID = "mycompany.myproduct.subproduct.version"
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except Exception:
        pass

from Src.Tools.TimeRecord import track_playtime
from Src.Tools.FormatTools import format_playtime
from Src.Tools.PathTools import *
from Src.Data.GameDataStore import GameDataStore
from Src.Services.SteamBackgroundService import SteamBackgroundService
from Src.BigPicture import BigPictureWindow, GamepadManager, trim_transparent
from Src.GUI.BackgroundWidget import (
    BackgroundPickerDialog, BackgroundWidget, cover_pixmap, draw_center_crop,
)
from Src.GUI.QSSStyle import STYLE_SHEET

from PyQt5.QtCore import (
    Qt, QSize, QSizeF, QUrl, QFileInfo, QTimer, QElapsedTimer, QDateTime,
    pyqtSignal, QRect,
)
from PyQt5.QtGui import (
    QIcon, QColor, QKeySequence, QFont, QPalette, QPixmap, QPainter,
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel,
    QFileDialog, QMessageBox, QDialog, QFormLayout, QDialogButtonBox,
    QFileIconProvider, QMenu, QShortcut,
    QFrame, QSlider, QInputDialog, QListWidget, QListWidgetItem,
    QSystemTrayIcon, QScrollArea,
    QStyle,
)


if sys.platform == "win32":
    BROWSE_FILTER = "游戏程序 (*.exe *.bat *.cmd *.com *.lnk)"
elif sys.platform == "darwin":
    BROWSE_FILTER = "应用程序 (*.app)"
else:
    BROWSE_FILTER = "可执行程序 (*)"


def _get_clean_qt_env():
    """返回一个移除了 PyInstaller 注入的 Qt 污染的环境变量副本。"""
    env = os.environ.copy()

    # 1. 移除 Qt 插件路径相关的变量，这是导致冲突的直接原因
    qt_vars_to_remove = (
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
    )
    for var in qt_vars_to_remove:
        env.pop(var, None)

    # 2. 从 PATH 中剔除 PyInstaller 的临时解压目录 (_MEIPASS)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        path_entries = env.get("PATH", "").split(os.pathsep)
        clean_path = [p for p in path_entries if meipass not in p]
        env["PATH"] = os.pathsep.join(clean_path)

    return env

def get_app_icon():
    icon = QIcon(APP_ICON)
    if not icon.isNull():
        return icon
    app = QApplication.instance()
    if app is not None:
        return app.style().standardIcon(QStyle.SP_ComputerIcon)
    return QIcon()


def apply_theme(app):
    """应用深色科技风调色板 + 样式表。"""
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#0b0f14"))
    pal.setColor(QPalette.WindowText, QColor("#d7e2ea"))
    pal.setColor(QPalette.Base, QColor("#0d1319"))
    pal.setColor(QPalette.AlternateBase, QColor("#0f161e"))
    pal.setColor(QPalette.Text, QColor("#d7e2ea"))
    pal.setColor(QPalette.Button, QColor("#121820"))
    pal.setColor(QPalette.ButtonText, QColor("#d7e2ea"))
    pal.setColor(QPalette.Highlight, QColor("#122e36"))
    pal.setColor(QPalette.HighlightedText, QColor("#a5f3fc"))
    pal.setColor(QPalette.ToolTipBase, QColor("#121820"))
    pal.setColor(QPalette.ToolTipText, QColor("#d7e2ea"))
    if hasattr(QPalette, "PlaceholderText"):
        pal.setColor(QPalette.PlaceholderText, QColor("#4f6070"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#4a5866"))

    app.setPalette(pal)
    app.setStyleSheet(STYLE_SHEET)


# ================================================================ 自定义背景
"""自定义背景对话框：选择图片 + 调整不透明度。"""
class BackgroundDialog(QDialog):

    PREVIEW_SIZE = QSize(440, 205)

    def __init__(self, parent=None, path="", opacity=DEFAULT_BG_OPACITY):
        super().__init__(parent)
        self.result_settings = None
        self.setWindowTitle("自定义背景")
        self.setMinimumWidth(540)
        self._opacity = float(opacity)
        self._preview_path = ""
        self._preview_pixmap = QPixmap()
        self._preview_cover = QPixmap()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 预览图
        self.preview = QLabel()
        self.preview.setObjectName("bgPreview")
        self.preview.setFixedSize(self.PREVIEW_SIZE)
        self.preview.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.preview, 0, Qt.AlignHCenter)

        # 图片路径
        self.path_edit = QLineEdit(path)
        self.path_edit.setPlaceholderText("选择一张图片作为主界面背景（png / jpg / bmp …）")
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self.browse)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        # 不透明度
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("背景不透明度："))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(10, 100)
        self.slider.setValue(int(round(self._opacity * 100)))
        self.slider.valueChanged.connect(self.on_opacity_changed)
        slider_row.addWidget(self.slider, 1)
        self.opacity_label = QLabel(f"{self.slider.value()}%")
        self.opacity_label.setMinimumWidth(42)
        self.opacity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider_row.addWidget(self.opacity_label)
        layout.addLayout(slider_row)

        tip = QLabel("背景会铺满主界面并叠加暗色蒙版，保证表格文字清晰可读。")
        tip.setObjectName("mutedLabel")
        layout.addWidget(tip)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("确定")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        clear_btn = btns.addButton("移除背景", QDialogButtonBox.ActionRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        clear_btn.clicked.connect(self.clear_background)
        layout.addWidget(btns)

        self.path_edit.editingFinished.connect(self.refresh_preview)
        self.refresh_preview()

    def browse(self):
        start_dir = os.path.expanduser("~")
        cur = self.path_edit.text().strip().strip('"')
        if cur and os.path.isdir(os.path.dirname(cur)):
            start_dir = os.path.dirname(cur)
        path, _ = QFileDialog.getOpenFileName(
            self, "选择背景图片", start_dir,
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
        if path:
            self.path_edit.setText(os.path.normpath(path))
            self.refresh_preview()

    def clear_background(self):
        self.path_edit.clear()
        self.refresh_preview()

    def on_opacity_changed(self, value):
        self._opacity = value / 100.0
        self.opacity_label.setText(f"{value}%")
        self.refresh_preview()

    def refresh_preview(self):
        path = self.path_edit.text().strip().strip('"')
        # 同一路径只从磁盘读取一次，拖动不透明度滑块时无需重复加载大图
        if path != self._preview_path:
            self._preview_path = path
            self._preview_pixmap = QPixmap(path) if path else QPixmap()
            self._preview_cover = cover_pixmap(
                self._preview_pixmap, self.preview.size())
        canvas = QPixmap(self.preview.size())
        canvas.fill(QColor("#0b0f14"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self._preview_cover.isNull():
            painter.setPen(QColor("#5f7080"))
            painter.drawText(canvas.rect(), Qt.AlignCenter, "未设置背景图片")
        else:
            draw_center_crop(painter, self._preview_cover, canvas.rect(),
                             self._opacity)
            painter.fillRect(canvas.rect(),
                             QColor(7, 11, 15, int(BG_VEIL_OPACITY * 255)))
        painter.end()
        self.preview.setPixmap(canvas)

    def accept(self):
        path = self.path_edit.text().strip().strip('"')
        if path:
            if not os.path.isfile(path):
                QMessageBox.warning(self, "提示", f"图片不存在：\n{path}")
                return
            if QPixmap(path).isNull():
                QMessageBox.warning(self, "提示", "无法读取该图片，请换一张试试。")
                return
        self.result_settings = {"path": path, "opacity": self._opacity}
        super().accept()


class ElidedLabel(QLabel):
    """单行文本标签：宽度不足时自动省略中间部分，避免撑开布局。"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setFullText(text)

    def setFullText(self, text):
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def minimumSizeHint(self):
        # 只保留文字高度，宽度允许任意收缩（实际显示时按宽度省略）
        return QSize(0, super().minimumSizeHint().height())

    def _refresh(self):
        width = max(0, self.width() - 2)
        self.setText(self.fontMetrics().elidedText(
            self._full_text, Qt.ElideMiddle, width))


class ShelfScrollArea(QScrollArea):
    """Switch 风格游戏架：隐藏滚动条，滚轮直接横向浏览。"""

    def wheelEvent(self, event):
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()


class GameTile(QFrame):
    """Switch 风格的游戏卡片：方形图标 + 名称 + 游玩时长。"""

    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()
    contextMenuRequested = pyqtSignal(object)      # 全局坐标 QPoint

    WIDTH = 180
    HEIGHT = 228
    ICON_BOX = 120

    def __init__(self, item, seconds, icon, parent=None):
        super().__init__(parent)
        self.setObjectName("gameTile")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setProperty("selected", "false")
        self.setProperty("hover", "false")
        self.setProperty("missing", "false")

        path = item.get("path", "") or ""
        missing = bool(path) and not os.path.exists(path)
        self.setToolTip(f"文件不存在：{path}" if missing else (path or "未设置程序路径"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)

        icon_label = QLabel()
        icon_label.setFixedSize(self.ICON_BOX, self.ICON_BOX)
        icon_label.setAlignment(Qt.AlignCenter)
        pixmap = icon.pixmap(256, 256) if icon is not None else QPixmap()
        if not pixmap.isNull():
            pixmap = trim_transparent(pixmap).scaled(
                self.ICON_BOX - 8, self.ICON_BOX - 8,
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        layout.addWidget(icon_label, 0, Qt.AlignHCenter)

        name_label = QLabel(item.get("name", "") or "未命名游戏")
        name_label.setObjectName("tileName")
        name_label.setAlignment(Qt.AlignCenter)
        name_label.setWordWrap(True)
        name_label.setFixedHeight(36)
        layout.addWidget(name_label)

        time_label = QLabel(format_playtime(seconds))
        time_label.setObjectName("tileTime")
        time_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(time_label)

        # 子控件不拦截鼠标，保证卡片整体的悬浮 / 点击判定始终生效
        for widget in (icon_label, name_label, time_label):
            widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        if missing:
            self._set_property("missing", True)

    def _set_property(self, name, enabled):
        value = "true" if enabled else "false"
        if self.property(name) != value:
            self.setProperty(name, value)
            self.style().unpolish(self)
            self.style().polish(self)

    def set_selected(self, selected):
        self._set_property("selected", selected)

    def _set_hovered(self, hovered):
        self._set_property("hover", hovered)

    def enterEvent(self, event):
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hovered(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        self.contextMenuRequested.emit(event.globalPos())
        event.accept()


"""主界面"""
class GameManager(QMainWindow):

    playtime_updated = pyqtSignal()
    # ★ [新增] 云同步完成信号：(云端数据 dict, 错误信息 str)
    cloud_sync_finished = pyqtSignal(object, str)
    # ★ [新增] 云端上传完成信号：(更新条数, 新增条数, 错误信息 str)
    cloud_upload_finished = pyqtSignal(int, int, str)
    # ★ [新增] 单个游戏背景变更完成（本地选择 / Steam 下载 / 修改 AppID）
    background_updated = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MP游戏启动器")
        self.resize(1180, 660)
        self.setMinimumSize(1120, 560)
        self.setWindowIcon(get_app_icon())

        self.items = []
        self._rows = []
        self._tiles = []
        self._row_seconds = []
        self._selected_index = -1
        self.icon_provider = QFileIconProvider()
        self._icon_cache = {}

        self._sort_column = None
        self._sort_order = Qt.AscendingOrder

        self.tray = None
        self._tray_tip_shown = False
        self._big_picture = None
        self._syncing = False
        self._uploading = False

        # 数据持久化与 Steam 背景服务（大屏模式也复用同一套接口）
        self.store = GameDataStore()
        self.bg_service = SteamBackgroundService(self.store, self)

        self.load_data()
        self.load_settings()
        self.load_steam_entries()
        self.init_ui()
        # ★ [新增] 启动时恢复用户上次设置的背景
        self.bg_widget.set_background(self._bg_path, self._bg_opacity)

        # 手柄：同时按住 LB + RB + LT + RT 切换大屏模式
        self.gamepad = GamepadManager(self)
        self.gamepad.combo_pressed.connect(self.toggle_big_picture)

        self.playtime_updated.connect(self.refresh_games)
        self.cloud_sync_finished.connect(self.on_cloud_synced)
        self.cloud_upload_finished.connect(self.on_cloud_uploaded)
        self.bg_service.fetched.connect(self.on_background_fetched)

        self.init_tray()
        self.refresh_games()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        # ★ [改动] 中央容器改为自绘背景 + 半透明内容控件的 BackgroundWidget
        central = BackgroundWidget()
        self.bg_widget = central
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(26, 16, 26, 12)
        root.setSpacing(12)

        # ---- 顶栏：Switch 风格状态栏（标题 + 时钟） ----
        title = QLabel("GAME TIME MANAGER")
        title.setObjectName("titleLabel")
        subtitle = QLabel("// 游戏时长管理平台")
        subtitle.setObjectName("subtitleLabel")
        self.clock_label = QLabel()
        self.clock_label.setObjectName("clockLabel")
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_row.addWidget(title)
        title_row.addWidget(subtitle, 0, Qt.AlignBottom)
        title_row.addStretch(1)
        title_row.addWidget(self.clock_label)
        root.addLayout(title_row)

        accent_line = QFrame()
        accent_line.setObjectName("accentLine")
        accent_line.setFixedHeight(2)
        root.addWidget(accent_line)

        # ---- 游戏架：单行横向卡片，始终位于窗口垂直中间 ----
        self.shelf_scroll = ShelfScrollArea()
        self.shelf_scroll.setObjectName("shelfScroll")
        self.shelf_scroll.setWidgetResizable(True)
        self.shelf_scroll.setFrameShape(QFrame.NoFrame)
        self.shelf_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.shelf_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.shelf_scroll.setFocusPolicy(Qt.NoFocus)
        self.shelf_scroll.viewport().setAutoFillBackground(False)

        self.shelf = QWidget()
        self.shelf.setObjectName("shelfContent")
        self.shelf.setAutoFillBackground(False)
        self.shelf_layout = QHBoxLayout(self.shelf)
        self.shelf_layout.setContentsMargins(18, 24, 18, 24)
        self.shelf_layout.setSpacing(18)
        self.shelf_layout.setAlignment(Qt.AlignCenter)
        self.shelf_scroll.setWidget(self.shelf)
        # setWidget() 会把内容控件设为自动填充背景（浅灰底），这里再次关闭以透出背景层
        self.shelf.setAutoFillBackground(False)
        root.addWidget(self.shelf_scroll, 1)

        # ---- 详情条：当前选中游戏的名称 / 路径 / 时长 / 背景操作 ----
        detail = QFrame()
        detail.setObjectName("detailBar")
        detail_layout = QHBoxLayout(detail)
        detail_layout.setContentsMargins(20, 12, 14, 12)
        detail_layout.setSpacing(14)

        info = QVBoxLayout()
        info.setSpacing(3)
        self.detail_name = QLabel("未选择游戏")
        self.detail_name.setObjectName("detailName")
        self.detail_path = ElidedLabel()
        self.detail_path.setObjectName("detailPath")
        info.addWidget(self.detail_name)
        info.addWidget(self.detail_path)
        detail_layout.addLayout(info, 1)

        self.detail_time = QLabel("")
        self.detail_time.setObjectName("detailTime")
        detail_layout.addWidget(self.detail_time)

        hint = QLabel("双击启动 · 单击切换背景 · 滚轮横向浏览")
        hint.setObjectName("mutedLabel")
        detail_layout.addWidget(hint)

        self.detail_steam_btn = QPushButton("⬇ Steam 背景")
        self.detail_steam_btn.setObjectName("accentBtn")
        self.detail_steam_btn.setToolTip(
            "左键：重新从 Steam 获取背景图 / 视频\n右键：修改 AppID")
        self.detail_local_btn = QPushButton("🖼 本地背景")
        self.detail_local_btn.setObjectName("accentBtn")
        self.detail_local_btn.setToolTip("选择本地图片或 mp4 视频作为该游戏的背景")
        for button in (self.detail_steam_btn, self.detail_local_btn):
            button.setMinimumHeight(34)
            button.setMinimumWidth(112)
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
        self.detail_steam_btn.clicked.connect(self.request_selected_background)
        self.detail_steam_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.detail_steam_btn.customContextMenuRequested.connect(
            self.edit_selected_appid)
        self.detail_local_btn.clicked.connect(self.choose_selected_background)
        detail_layout.addWidget(self.detail_steam_btn)
        detail_layout.addWidget(self.detail_local_btn)
        root.addWidget(detail)

        # ---- 底部操作栏 ----
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_add = QPushButton("＋ 登记游戏")
        self.btn_add.setObjectName("accentBtn")
        self.btn_edit = QPushButton("✎ 编辑")
        self.btn_del = QPushButton("🗑 删除")
        self.btn_open = QPushButton("▶ 启动")
        self.btn_open.setObjectName("primaryBtn")
        self.btn_big = QPushButton("🖥 大屏")
        self.btn_big.setObjectName("accentBtn")
        # ★ [新增] 自定义主界面背景
        self.btn_bg = QPushButton("🖼 背景")
        self.btn_bg.setObjectName("accentBtn")
        # ★ [新增] 从云端（飞书多维表格）同步游戏时长
        self.btn_sync = QPushButton("☁ 同步")
        self.btn_sync.setObjectName("accentBtn")
        # ★ [新增] 把本地最新时长上传到云端表格
        self.btn_upload = QPushButton("⬆ 上传")
        self.btn_upload.setObjectName("accentBtn")
        self.btn_sort = QPushButton("⇅ 排序")
        for b in (self.btn_add, self.btn_edit, self.btn_del, self.btn_open,
                  self.btn_big, self.btn_bg, self.btn_sync, self.btn_upload,
                  self.btn_sort):
            b.setMinimumHeight(38)
            b.setMinimumWidth(86)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_add.clicked.connect(self.add_item)
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_open.clicked.connect(self.launch_selected_row)
        self.btn_big.clicked.connect(self.open_big_picture)
        self.btn_bg.clicked.connect(self.choose_background)
        self.btn_sync.clicked.connect(self.sync_cloud)
        self.btn_upload.clicked.connect(self.upload_cloud)
        self.btn_sort.clicked.connect(self.show_sort_menu)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索游戏名称 / 路径…")
        self.search_edit.setMinimumHeight(38)
        self.search_edit.setMinimumWidth(200)
        self.search_edit.textChanged.connect(self.refresh_games)

        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_edit)
        bar.addWidget(self.btn_del)
        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_big)
        bar.addWidget(self.btn_bg)
        bar.addWidget(self.btn_sync)
        bar.addWidget(self.btn_upload)
        bar.addStretch(1)
        bar.addWidget(self.btn_sort)
        bar.addWidget(self.search_edit)
        root.addLayout(bar)

        QShortcut(QKeySequence("F11"), self, self.open_big_picture)
        QShortcut(QKeySequence("Return"), self, self._launch_from_shortcut)

        # 顶栏时钟
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

    def _update_clock(self):
        self.clock_label.setText(
            QDateTime.currentDateTime().toString("yyyy-MM-dd  HH:mm"))

    def show_sort_menu(self):
        """点击「排序」按钮：选择排序方式（替代原表头点击排序）。"""
        menu = QMenu(self)
        options = (
            ("登记顺序", None, Qt.AscendingOrder),
            ("名称 A → Z", 0, Qt.AscendingOrder),
            ("名称 Z → A", 0, Qt.DescendingOrder),
            ("时长 少 → 多", 1, Qt.AscendingOrder),
            ("时长 多 → 少", 1, Qt.DescendingOrder),
        )
        for label, column, order in options:
            action = menu.addAction(label)
            action.setCheckable(True)
            checked = self._sort_column == column
            if checked and column is not None:
                checked = self._sort_order == order
            action.setChecked(checked)
            action.triggered.connect(
                lambda _checked=False, c=column, o=order: self.set_sort(c, o))
        menu.exec_(self.btn_sort.mapToGlobal(self.btn_sort.rect().bottomLeft()))

    def set_sort(self, column, order):
        self._sort_column = column
        if column is not None:
            self._sort_order = order
        self.refresh_games()

    # ------------------------------------------------------------ 系统托盘
    def init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(get_app_icon())
        self.tray.setToolTip("MP游戏启动器（双击恢复）")

        menu = QMenu(self)
        act_show = menu.addAction("显示主窗口")
        act_show.triggered.connect(self.show_window)
        menu.addSeparator()
        act_quit = menu.addAction("退出程序")
        act_quit.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)

        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if self.tray is not None and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self._tray_tip_shown:
                self._tray_tip_shown = True
                self.tray.showMessage(
                    "仍在后台运行",
                    "程序已最小化到系统托盘，双击托盘图标可重新打开。",
                    QSystemTrayIcon.Information,
                    3000,
                )
        else:
            event.accept()
            QTimer.singleShot(0, QApplication.quit)

    # ------------------------------------------------------------ 图标
    def get_icon(self, path):
        if path in self._icon_cache:
            return self._icon_cache[path]
        icon = QIcon()
        fi = QFileInfo(path)
        if fi.exists():
            try:
                icon = self.icon_provider.icon(fi)
            except Exception:
                pass
        self._icon_cache[path] = icon
        return icon

    # ------------------------------------------------------------ 数据读写
    def load_data(self):
        try:
            self.items = self.store.load_items()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"配置文件读取失败：\n{e}")
            self.items = []

    def save_data(self):
        try:
            self.store.save_items(self.items)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"配置写入失败：\n{e}")

    # ------------------------------------------------------------ 自定义背景
    def load_settings(self):
        """读取界面外观设置（背景路径 / 不透明度）。"""
        self._bg_path, self._bg_opacity = self.store.load_settings()

    def save_settings(self):
        try:
            self.store.save_settings(self._bg_path, self._bg_opacity)
        except OSError:
            pass

    def choose_background(self):
        """打开自定义背景对话框，确认后立即应用并保存设置。"""
        dlg = BackgroundDialog(self, self._bg_path, self._bg_opacity)
        if dlg.exec_() != QDialog.Accepted or not dlg.result_settings:
            return
        path = dlg.result_settings.get("path", "")
        self._bg_opacity = dlg.result_settings.get("opacity", DEFAULT_BG_OPACITY)
        self._bg_path = path if path and os.path.isfile(path) else ""
        self.bg_widget.set_background(self._bg_path, self._bg_opacity)
        self.save_settings()
        self.statusBar().showMessage(
            "已设置自定义背景" if self._bg_path else "已移除自定义背景", 3000)

    # ------------------------------------------------------------ Steam 背景
    def load_steam_entries(self):
        """读取 Steam AppID / 背景缓存记录（键为游戏 exe 文件名小写）。"""
        try:
            self.bg_service.load_entries()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"Steam 背景配置读取失败：\n{e}")

    def get_global_background(self):
        """返回主界面全局背景设置，供大屏模式作为兜底背景使用。"""
        return self._bg_path, self._bg_opacity

    def get_game_background(self, item):
        """返回单个游戏的 (视频路径, 图片路径)，供大屏模式复用。"""
        return self.bg_service.get_game_background(item)

    def _selected_item(self):
        if 0 <= self._selected_index < len(self._rows):
            return self._rows[self._selected_index]
        return None

    def select_index(self, index, apply_background=False, scroll=True):
        """选中游戏架中的第 index 张卡片，并同步详情条。"""
        if not (0 <= index < len(self._rows)):
            return
        self._selected_index = index
        for i, tile in enumerate(self._tiles):
            tile.set_selected(i == index)
        self._update_detail()
        if scroll and index < len(self._tiles):
            self.shelf_scroll.ensureWidgetVisible(self._tiles[index], 60, 0)
        if apply_background:
            self.apply_game_background(self._rows[index])

    def select_relative(self, delta):
        """键盘左右方向键在当前游戏架中移动选中项。"""
        if not self._rows:
            return
        index = self._selected_index if self._selected_index >= 0 else 0
        self.select_index(max(0, min(len(self._rows) - 1, index + delta)))

    def _update_detail(self):
        """刷新详情条：选中游戏的名称 / 路径 / 时长 / 背景按钮状态。"""
        item = self._selected_item()
        if item is None:
            self.detail_name.setText("未选择游戏")
            self.detail_path.setFullText("")
            self.detail_time.setText("")
            self.detail_steam_btn.setEnabled(False)
            self.detail_local_btn.setEnabled(False)
            return

        path = item.get("path", "") or ""
        text = path or "未设置程序路径"
        if path and not os.path.exists(path):
            text = f"文件不存在：{path}"
        self.detail_name.setText(item.get("name", "") or "未命名游戏")
        self.detail_path.setFullText(text)
        seconds = (self._row_seconds[self._selected_index]
                   if self._selected_index < len(self._row_seconds) else 0.0)
        self.detail_time.setText(format_playtime(seconds))

        fetching = self.bg_service.is_fetching(item.get("id"))
        self.detail_steam_btn.setEnabled(not fetching)
        self.detail_steam_btn.setText("… 获取中" if fetching else "⬇ Steam 背景")
        self.detail_local_btn.setEnabled(True)

    def request_selected_background(self, _checked=False):
        item = self._selected_item()
        if item is not None:
            self.request_steam_background(item)

    def edit_selected_appid(self, _pos=None):
        item = self._selected_item()
        if item is not None:
            self.edit_steam_appid(item)

    def choose_selected_background(self, _checked=False):
        item = self._selected_item()
        if item is not None:
            self.choose_custom_background(item)

    def apply_game_background(self, item):
        # 大屏正在显示时由大屏自己的背景层渲染，避免同一视频被解码两次
        if self._big_picture is not None and self._big_picture.isVisible():
            return
        video, image = self.bg_service.get_game_background(item)
        if video and self.bg_widget.play_video(video, image or self._bg_path):
            self.statusBar().showMessage(f"背景：{item.get('name', '')}", 3000)
            return
        if image:
            self.bg_widget.set_background(image, self._bg_opacity)
            self.statusBar().showMessage(f"背景：{item.get('name', '')}", 3000)
            return
        self.bg_widget.set_background(self._bg_path, self._bg_opacity)
        self.statusBar().showMessage(
            "该游戏还没有背景，点击「Steam 背景」下载或「本地背景」选择文件", 4000)

    def request_steam_background(self, item, parent=None):
        """详情条「Steam 背景」按钮：每次点击都重新下载该游戏的背景图 / 视频。"""
        item_id = item.get("id", "")
        if not item_id:
            return
        if self.bg_service.is_fetching(item_id):
            self.statusBar().showMessage("正在获取背景，请稍候…", 3000)
            return
        dlg_parent = parent or self
        entry = self.bg_service.entry(item)
        appid = str(entry.get("appid", "") or "").strip()
        if not appid:
            appid, ok = QInputDialog.getText(
                dlg_parent, "Steam AppID",
                f"请输入「{item.get('name', '')}」的 Steam AppID（数字）：")
            if not ok:
                return
            appid = appid.strip()
            if not appid.isdigit():
                QMessageBox.warning(dlg_parent, "提示", "AppID 必须是纯数字。")
                return
            record = self.bg_service.ensure_entry(item)
            record["appid"] = int(appid)
            self.bg_service.save_entries()

        # 不读取缓存，强制重新请求接口并覆盖本地文件
        self._start_bg_download(item, appid, parent)

    def edit_steam_appid(self, item, parent=None):
        """右键 Steam 按钮：修改 AppID，并立即按新 AppID 重新获取背景。"""
        dlg_parent = parent or self
        entry = self.bg_service.entry(item)
        current = str(entry.get("appid", "") or "").strip()
        appid, ok = QInputDialog.getText(
            dlg_parent, "修改 Steam AppID",
            f"「{item.get('name', '')}」当前 AppID：{current or '未设置'}\n"
            "请输入新的 AppID（数字）：", text=current)
        if not ok:
            return
        appid = appid.strip()
        if not appid.isdigit():
            QMessageBox.warning(dlg_parent, "提示", "AppID 必须是纯数字。")
            return
        if appid == current:
            return

        # 更新 AppID 并清掉旧 AppID 下载到本地的背景，避免继续显示另一款游戏的画面
        self.bg_service.set_appid(item, appid)
        self.apply_game_background(item)
        self.background_updated.emit(item)

        self._start_bg_download(item, appid, parent)
        self.statusBar().showMessage(f"AppID 已改为 {appid}，正在重新获取背景…")

    def _start_bg_download(self, item, appid, parent=None):
        """启动后台下载，并记录本次请求的 AppID 以便丢弃过期结果。"""
        item_id = item.get("id", "")
        if not item_id:
            return
        # 记录弹窗父窗口：大屏模式下弹窗必须显示在大屏窗口之上
        self.bg_service.start_fetch(item_id, appid, parent)
        self._update_detail()
        self.statusBar().showMessage("正在从 Steam 获取背景…")

    def choose_custom_background(self, item, parent=None):
        """详情条「本地背景」按钮：为单个游戏指定本地图片 / 视频作为背景。"""
        # 起始目录：该游戏从 Steam 下载的背景图所在目录
        entry = self.bg_service.entry(item)
        appid = str(entry.get("appid", "") or "").strip()
        gallery_dir = os.path.join(STEAM_MEDIA_DIR, f"{appid}_gallery") if appid else ""
        if not gallery_dir or not os.path.isdir(gallery_dir):
            os.makedirs(STEAM_MEDIA_DIR, exist_ok=True)
            gallery_dir = STEAM_MEDIA_DIR
        path, _ = QFileDialog.getOpenFileName(
            parent or self, "选择背景文件", gallery_dir,
            "媒体文件 (*.png *.jpg *.jpeg *.bmp *.webp *.mp4)")
        if not path:
            return

        self.bg_service.set_local_background(item, path)
        self.apply_game_background(item)
        self.background_updated.emit(item)
        self.statusBar().showMessage(f"已使用本地背景：{os.path.basename(path)}", 4000)

    @staticmethod
    def _alive_widget(widget, fallback):
        """弹窗父窗口可能在大屏关闭后被销毁 / 隐藏，此时回退到主窗口。"""
        if widget is None:
            return fallback
        try:
            if widget.isVisible():
                return widget
        except RuntimeError:
            pass
        return fallback

    def on_background_fetched(self, item_id, result, error):
        """主线程：保存下载结果并立即应用背景。"""
        pending = self.bg_service.pending(item_id)
        pending_appid = str(pending.get("appid", "") or "")
        dlg_parent = self._alive_widget(pending.get("parent"), self)
        result_appid = str((result or {}).get("appid", "") or "")
        # 用户已修改 AppID 时，旧请求的回调直接丢弃
        if not pending or (
                result_appid and result_appid != pending_appid):
            return
        self.bg_service.take_pending(item_id)
        if error or not result:
            self.refresh_games()
            self.statusBar().showMessage("Steam 背景获取失败", 4000)
            QMessageBox.warning(
                dlg_parent, "获取失败", f"无法从 Steam 获取背景：\n{error}")
            return

        item = next((it for it in self.items if it.get("id") == item_id), None)
        if item is None:
            self.refresh_games()
            return

        entry = self.bg_service.ensure_entry(item)
        entry.update({
            "appid": result.get("appid", entry.get("appid")),
            "name": result.get("name") or entry.get("name", ""),
            "image": result.get("image", ""),
            "video": result.get("video", ""),
        })
        self.bg_service.save_entries()
        self.refresh_games()

        # 下载到的背景图多于一张时，弹窗让用户逐张预览并选择
        images = [
            img for img in (result.get("images") or [])
            if img.get("path") and os.path.isfile(img["path"])
        ]
        chosen = ""
        if len(images) > 1:
            dialog = BackgroundPickerDialog(
                images, entry.get("name") or item.get("name", ""), dlg_parent)
            if dialog.exec_() == QDialog.Accepted:
                chosen = dialog.selected_path()
            dialog.deleteLater()

        if chosen:
            entry["image"] = chosen
            # 选定静态背景后停用视频，确保所选图片真正生效
            entry["video"] = ""
            self.bg_service.save_entries()

        self.apply_game_background(item)
        self.background_updated.emit(item)
        if chosen:
            self.statusBar().showMessage("已应用所选背景", 4000)
        else:
            self.statusBar().showMessage(
                f"已获取背景：{result.get('name') or item.get('name', '')}", 4000)

    def read_local_playtime(self):
        """读取本地时长原始记录（保留原始进程名大小写）。"""
        return self.store.read_playtime()

    def load_playtime(self):
        return self.store.load_playtime()

    # ------------------------------------------------------------ 云同步
    def sync_cloud(self):
        """点击「同步」：后台线程拉取飞书多维表格中的游戏时长。"""
        if self._syncing:
            return
        self._syncing = True
        self.btn_sync.setEnabled(False)
        self.btn_sync.setText("☁ 同步中…")
        self.statusBar().showMessage("正在从云端同步游戏时长…")
        threading.Thread(target=self._fetch_cloud_playtime, daemon=True).start()

    def _fetch_cloud_playtime(self):
        """子线程：调用 SearchTableAPI，结果通过信号回到主线程。"""
        try:
            from Src.FeishuAPI.SearchTableAPI import SearchTable
            data = SearchTable() or {}
            if not isinstance(data, dict):
                raise ValueError("云端返回的数据格式不正确")
            self.cloud_sync_finished.emit(data, "")
        except Exception as e:
            self.cloud_sync_finished.emit({}, str(e))

    def on_cloud_synced(self, data, error):
        """主线程：恢复按钮并合并云端时长到本地 playtime.json。"""
        self._syncing = False
        self.btn_sync.setEnabled(True)
        self.btn_sync.setText("☁ 同步")

        if error:
            self.statusBar().showMessage("云同步失败", 4000)
            QMessageBox.warning(self, "云同步失败", f"无法获取云端数据：\n{error}")
            return
        if not data:
            self.statusBar().showMessage("云端暂无时长数据", 4000)
            return

        updated = self.merge_playtime(data)
        self.refresh_games()
        self.statusBar().showMessage(
            f"云同步完成：云端 {len(data)} 条记录，更新本地 {updated} 条", 5000)

    def merge_playtime(self, cloud):
        """把云端时长合并进本地记录：同名游戏取较大值，保留本地独有记录。"""
        try:
            return self.store.merge_playtime(cloud)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"本地时长写入失败：\n{e}")
            return 0

    # ------------------------------------------------------------ 云端上传
    def upload_cloud(self):
        """点击「上传」：确认后把本地时长写入云端表格。"""
        if self._uploading:
            return
        local = self.read_local_playtime()
        if not local:
            QMessageBox.information(self, "提示", "本地暂无可上传的时长记录。")
            return
        ret = QMessageBox.question(
            self, "确认上传",
            f"把本地 {len(local)} 条游戏时长上传到云端表格？\n\n"
            "云端已有同名游戏会被更新为本地时长，本地独有的游戏会新增记录。")
        if ret != QMessageBox.Yes:
            return

        self._uploading = True
        self.btn_upload.setEnabled(False)
        self.btn_upload.setText("⬆ 上传中…")
        self.statusBar().showMessage("正在上传本地时长到云端表格…")
        threading.Thread(target=self._do_upload, args=(local,), daemon=True).start()

    def _do_upload(self, local):
        """子线程：比对云端记录后逐条更新 / 新增，结果通过信号回到主线程。"""
        updated = 0
        added = 0
        try:
            from Src.FeishuAPI.SearchTableAPI import create_client, SearchTableRecords
            from Src.FeishuAPI.UpdateTableAPI import UpdateTable
            from Src.FeishuAPI.AddRecordAPI import AddRecord

            client = create_client()
            cloud = SearchTableRecords(client)

            for name, raw in local.items():
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue

                exist = cloud.get(str(name).lower())
                if exist:
                    record_id, cloud_value = exist
                    try:
                        unchanged = abs(float(cloud_value) - value) < 1e-6
                    except (TypeError, ValueError):
                        unchanged = False
                    if unchanged:
                        continue
                    if UpdateTable(record_id, name, value, client):
                        updated += 1
                elif AddRecord(name, value, client):
                    added += 1

            self.cloud_upload_finished.emit(updated, added, "")
        except Exception as e:
            self.cloud_upload_finished.emit(updated, added, str(e))

    def on_cloud_uploaded(self, updated, added, error):
        """主线程：恢复按钮并提示上传结果。"""
        self._uploading = False
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("⬆ 上传")

        if error:
            self.statusBar().showMessage("云端上传失败", 4000)
            QMessageBox.warning(
                self, "云端上传失败",
                f"上传过程中出现问题：\n{error}\n\n"
                f"已更新 {updated} 条，新增 {added} 条。")
            return

        self.statusBar().showMessage(
            f"上传完成：更新 {updated} 条，新增 {added} 条", 5000)

    # ------------------------------------------------------------ 刷新游戏架
    def refresh_games(self):
        """按搜索 / 排序条件重建 Switch 风格横向游戏架。"""
        kw = self.search_edit.text().strip().lower() if hasattr(self, "search_edit") else ""

        playtime = self.load_playtime()

        # 第一步：收集所有满足搜索条件的游戏（含原始秒数）
        display = []                          # [(item, seconds), ...]
        for item in self.items:
            name = item.get("name", "")
            path = item.get("path", "")
            if kw and (kw not in name.lower()
                       and kw not in path.lower()):
                continue

            exe_name = os.path.basename(path.strip().strip('"')).lower()
            raw = playtime.get(exe_name, 0)
            # playtime.json 中若存了字符串数字，统一转成 float
            try:
                seconds = float(raw)
            except (TypeError, ValueError):
                seconds = 0.0

            display.append((item, seconds))

        # 第二步：按当前排序状态排序（时长按数值排序）
        if self._sort_column is not None:
            reverse = (self._sort_order == Qt.DescendingOrder)
            if self._sort_column == 1:
                display.sort(key=lambda x: x[1], reverse=reverse)
            elif self._sort_column == 0:
                display.sort(
                    key=lambda x: str(x[0].get("name", "")).lower(),
                    reverse=reverse,
                )

        # 记录刷新前的选中项，重建后按 id 恢复
        previous = self._selected_item()
        previous_id = previous.get("id", "") if previous else ""

        # 第三步：清空旧卡片并重建
        while self.shelf_layout.count():
            layout_item = self.shelf_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

        self._rows = []
        self._tiles = []
        self._row_seconds = []

        if not display:
            empty = QLabel("暂无已登记的游戏\n点击下方「＋ 登记游戏」开始")
            empty.setObjectName("shelfEmpty")
            empty.setAlignment(Qt.AlignCenter)
            self.shelf_layout.addWidget(empty, 0, Qt.AlignCenter)
            self._selected_index = -1
            self._update_detail()
        else:
            for index, (item, seconds) in enumerate(display):
                tile = GameTile(item, seconds, self.get_icon(item.get("path", "")))
                tile.clicked.connect(
                    lambda i=index: self.select_index(i, apply_background=True))
                tile.doubleClicked.connect(lambda i=index: self.launch_row(i))
                tile.contextMenuRequested.connect(
                    lambda pos, it=item: self.show_menu(it, pos))
                self.shelf_layout.addWidget(tile, 0, Qt.AlignCenter)
                self._rows.append(item)
                self._tiles.append(tile)
                self._row_seconds.append(seconds)

            restore = 0
            for index, item in enumerate(self._rows):
                if item.get("id") == previous_id:
                    restore = index
                    break
            self._selected_index = -1
            self.select_index(restore, scroll=False)

        self.statusBar().showMessage(
            f"共 {len(self.items)} 个游戏，当前显示 {len(self._rows)} 个"
        )

    # ------------------------------------------------------------ 增 / 改 / 删
    def add_item(self):
        dlg = GameDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            data = dict(dlg.result_data)
            data["id"] = uuid.uuid4().hex
            self.items.append(data)
            self.save_data()
            self.refresh_games()

    def _selected_rows(self):
        return [self._selected_index] if self._selected_item() is not None else []

    def edit_selected(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要编辑的游戏。")
            return
        item = self._rows[rows[0]]
        dlg = GameDialog(self, item)
        if dlg.exec_() == QDialog.Accepted:
            item.update(dlg.result_data)
            self.save_data()
            self.refresh_games()

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要删除的游戏。")
            return
        names = "、".join(self._rows[r].get("name", "") for r in rows)
        ret = QMessageBox.question(
            self, "确认删除", f"确定要删除以下登记项吗？\n\n{names}")
        if ret != QMessageBox.Yes:
            return
        del_ids = {self._rows[r]["id"] for r in rows}
        self.items = [it for it in self.items if it["id"] not in del_ids]
        self.save_data()
        self.refresh_games()

    # ------------------------------------------------------------ 启动
    def launch_selected_row(self):
        rows = self._selected_rows()
        if not rows:
            return
        self.launch_row(rows[0])

    def launch_row(self, row):
        if row < 0 or row >= len(self._rows):
            return
        self.launch(self._rows[row])

    # ------------------------------------------------------------ 大屏模式
    def open_big_picture(self):
        if self._big_picture is not None and self._big_picture.isVisible():
            self._big_picture.raise_()
            self._big_picture.activateWindow()
            return
        playtime = self.load_playtime()
        win = BigPictureWindow(
            self, self.items, playtime, self.get_icon, self.launch,
            gamepad=self.gamepad, background_api=self)
        self._big_picture = win

    def toggle_big_picture(self):
        if self._big_picture is not None and self._big_picture.isVisible():
            self._big_picture.close()
        else:
            self.open_big_picture()

    def launch(self, item):
        path = item.get("path", "").strip().strip('"')
        exe_name = os.path.basename(path)
        args = item.get("args", "").strip()
        if not path:
            return
        if not os.path.exists(path):
            QMessageBox.warning(
                self, "无法启动",
                f"文件不存在：\n{path}\n\n请检查游戏是否已被移动或卸载。")
            return
        if not is_program_file(path):
            QMessageBox.warning(
                self, "无法启动",
                f"该路径不是可执行程序：\n{path}")
            return

        cwd = os.path.dirname(path) or None
        _log = self._launch_log  # 下面定义
        _log(f"=== launch() called ===")
        _log(f"  path       = {path}")
        _log(f"  exe_name   = {exe_name}")
        _log(f"  args       = {args!r}")
        _log(f"  cwd        = {cwd}")
        _log(f"  sys.executable = {sys.executable}")
        _log(f"  frozen?    = {getattr(sys, 'frozen', False)}")
        _log(f"  platform   = {sys.platform}")
        _log(f"  os.path.exists(path) = {os.path.exists(path)}")
        _log(f"  os.path.exists(cwd)  = {os.path.exists(cwd) if cwd else 'N/A'}")
        _log(f"  os.path.isfile(path) = {os.path.isfile(path)}")

        try:
            threading.Thread(
                target=track_playtime,
                args=(self.playtime_updated.emit, exe_name, PLAY_TIME_FILE, 5),
                daemon=True,
            ).start()

            if sys.platform == "win32":
                extra = shlex.split(args) if args else []
                proc = subprocess.Popen(
                    [path] + extra,
                    cwd=cwd,
                    shell=False,
                    creationflags=0x00000000,
                    env=_get_clean_qt_env(),  # <--- 关键：传入清理过的环境
                )
                _log(f"  Popen succeeded, pid={proc.pid}, returncode={proc.returncode}")
            elif args:
                subprocess.Popen([path] + shlex.split(args), cwd=cwd)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path], cwd=cwd)
            else:
                subprocess.Popen(["xdg-open", path], cwd=cwd)

            self.statusBar().showMessage(f"已启动：{item.get('name', '')}", 4000)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            _log(f"  [EXCEPTION] {type(e).__name__}: {e}\n{tb}")
            QMessageBox.critical(self, "启动失败", f"无法启动该游戏：\n{e}")

    def _launch_log(self, msg):
        """打包后 print 被丢弃，统一落盘到 ~/.game_manager_launch.log。"""
        import datetime
        try:
            log_path = os.path.join(os.path.expanduser("~"), ".game_manager_launch.log")
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    # ------------------------------------------------------------ 右键菜单
    def show_menu(self, item, global_pos):
        try:
            index = next(i for i, it in enumerate(self._rows) if it is item)
        except StopIteration:
            return
        self.select_index(index)

        menu = QMenu(self)
        act_open = menu.addAction("▶ 启动")
        act_folder = menu.addAction("📂 打开所在文件夹")
        menu.addSeparator()
        act_edit = menu.addAction("✎ 编辑")
        act_del = menu.addAction("🗑 删除")

        action = menu.exec_(global_pos)

        if action == act_open:
            self.launch_row(index)
        elif action == act_folder:
            self.open_folder(item.get("path", ""))
        elif action == act_edit:
            self.edit_selected()
        elif action == act_del:
            self.delete_selected()

    def keyPressEvent(self, event):
        """键盘操作：左右方向键在当前游戏架中移动选中项。"""
        if isinstance(QApplication.focusWidget(), QLineEdit):
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key_Left:
            self.select_relative(-1)
            return
        if key == Qt.Key_Right:
            self.select_relative(1)
            return
        super().keyPressEvent(event)

    def _launch_from_shortcut(self):
        """Enter 启动选中游戏；输入框聚焦时不触发。"""
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return
        self.launch_selected_row()

    def open_folder(self, path):
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "提示", "文件不存在，无法定位。")
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

"""游戏登记对话框"""
class GameDialog(QDialog):
    """新增 / 编辑游戏信息（仅允许选择程序）"""

    def __init__(self, parent=None, data=None):
        super().__init__(parent)
        self.result_data = {}
        self.setWindowTitle("编辑游戏" if data else "登记游戏")
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.name_edit = QLineEdit((data or {}).get("name", ""))
        self.name_edit.setPlaceholderText("例如：原神、赛博朋克2077")

        self.path_edit = QLineEdit((data or {}).get("path", ""))
        self.path_edit.setPlaceholderText(r"例如：D:\Games\xxx\xxx.exe")
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self.browse)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        path_widget = QWidget()
        path_widget.setLayout(path_row)

        self.args_edit = QLineEdit((data or {}).get("args", ""))
        self.args_edit.setPlaceholderText("可选，例如：-windowed -novid")

        self.desc_edit = QLineEdit((data or {}).get("desc", ""))
        self.desc_edit.setPlaceholderText("可选，例如：Steam 版 / 单机 / 联机")

        form.addRow("游戏名称：", self.name_edit)
        form.addRow("程序路径：", path_widget)
        form.addRow("启动参数：", self.args_edit)
        form.addRow("备注说明：", self.desc_edit)
        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("确定")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def browse(self):
        start_dir = os.path.expanduser("~")
        cur = self.path_edit.text().strip()
        if cur and os.path.isdir(os.path.dirname(cur)):
            start_dir = os.path.dirname(cur)

        path, _ = QFileDialog.getOpenFileName(
            self, "选择要登记的游戏程序", start_dir, BROWSE_FILTER)
        if not path:
            return
        path = os.path.normpath(path)
        if not is_program_file(path):
            QMessageBox.warning(
                self, "不是游戏程序",
                "请选择游戏可执行程序（如 .exe / .bat / .lnk 等），"
                "不要选择普通文档或文件夹。")
            return
        self.path_edit.setText(path)
        if not self.name_edit.text().strip():
            self.name_edit.setText(os.path.splitext(os.path.basename(path))[0])

    def accept(self):
        name = self.name_edit.text().strip()
        path = self.path_edit.text().strip().strip('"')
        if not name:
            QMessageBox.warning(self, "提示", "请填写游戏名称！")
            return
        if not path:
            QMessageBox.warning(self, "提示", "请选择游戏程序路径！")
            return

        if os.path.exists(path):
            if not is_program_file(path):
                QMessageBox.warning(
                    self, "不是游戏程序",
                    f"该路径不是可执行程序：\n{path}\n\n"
                    "本工具只登记游戏程序，不登记普通文件。")
                return
        else:
            ret = QMessageBox.question(
                self, "提示", f"该路径当前不存在：\n{path}\n\n仍然保存吗？")
            if ret != QMessageBox.Yes:
                return

        self.result_data = {
            "name": name,
            "path": path,
            "args": self.args_edit.text().strip(),
            "desc": self.desc_edit.text().strip(),
        }
        super().accept()




"""Main"""
def Main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setWindowIcon(get_app_icon())
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    win = GameManager()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    Main()