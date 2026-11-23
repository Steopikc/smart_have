from __future__ import annotations

import faulthandler
faulthandler.enable()

import sys
import sqlite3
import traceback
from pathlib import Path
from datetime import date, timedelta

from PySide6.QtCore import Qt, QDate, QTimer, QThread, Signal, QObject, QSize
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QStackedWidget, QComboBox, QDateEdit,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QMessageBox, QLineEdit, QScrollArea, QFileDialog, QDialog, QDialogButtonBox,
    QFormLayout, QRadioButton, QSpinBox, QPlainTextEdit, QSizePolicy
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ai_connector import (
    build_context_from_db, get_ai_response, check_llm_available, clear_caches,
)
from data_io import (
    import_database, import_sql_script, validate_db, list_serial_ports,
    read_serial_lines, parse_sensor_line, parse_csv_block, insert_sensor_rows,
)

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'digital_hive.db'
# [25 BEES] сначала пробуем новую базу
SQL_CANDIDATES = [
    ROOT / 'basafor25bees.sql',
    ROOT / 'smart_apiary_100_hives_hourly.sql',
    ROOT / 'pensil.sql',
]

P = '#F4511E'
P_DARK = '#D84315'
P_SOFT = '#FFF0EB'
O = '#DC2626'
O_SOFT = '#FEF2F2'
INK = '#171B2B'
MUTED = '#737887'
BG = '#F4F5F9'
BORDER = '#E6E8F0'


def connect(path):
    """PRAGMA для работы с БД на миллионы строк."""
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    for pragma in (
        'PRAGMA cache_size=-64000',
        'PRAGMA mmap_size=268435456',
        'PRAGMA temp_store=MEMORY',
    ):
        try:
            c.execute(pragma)
        except sqlite3.Error:
            pass
    return c


def _find_sql():
    for p in SQL_CANDIDATES:
        if p.exists():
            return p
    return None


def init_db():
    def _ok(path):
        try:
            c = connect(path)
            try:
                c.execute('SELECT 1 FROM hive_sensor_logs LIMIT 1')
                return True
            finally:
                c.close()
        except sqlite3.Error:
            return False

    if DB.exists() and _ok(DB):
        return

    sql_path = _find_sql()
    if sql_path is None:
        # [25 BEES] обновлена демо-схема: pesticide_risk, hive_health_index
        c = sqlite3.connect(str(DB))
        c.executescript('''
            CREATE TABLE IF NOT EXISTS apiaries (apiary_id INTEGER PRIMARY KEY, name TEXT NOT NULL, district TEXT);
            CREATE TABLE IF NOT EXISTS hives (hive_id INTEGER PRIMARY KEY, apiary_id INTEGER NOT NULL, hive_code TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hive_sensor_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT, hive_id INTEGER NOT NULL,
                record_date TEXT NOT NULL, record_datetime TEXT,
                internal_temp_c REAL DEFAULT 0, internal_humidity_pct REAL DEFAULT 0,
                co2_ppm REAL DEFAULT 0, hive_weight_kg REAL DEFAULT 0,
                daily_weight_delta_kg REAL DEFAULT 0, hourly_weight_delta_kg REAL DEFAULT 0,
                lid_opened_count INTEGER DEFAULT 0,
                lid_event_cause TEXT DEFAULT 'Нет', is_tipped_over INTEGER DEFAULT 0,
                tipping_cause TEXT DEFAULT 'Нет', sound_level_db REAL DEFAULT 0,
                bee_activity_index REAL DEFAULT 0, estimated_flights_count INTEGER DEFAULT 0,
                pesticide_risk INTEGER DEFAULT 0, hive_health_index REAL DEFAULT 5.0,
                ambient_temp_c REAL DEFAULT 0, anomaly_flag INTEGER DEFAULT 0, anomaly_description TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_weather (
                apiary_id INTEGER, record_date TEXT, ambient_temp_c REAL, ambient_humidity_pct REAL,
                precipitation_mm REAL, wind_speed_ms REAL, weather_condition TEXT,
                forecast_next_day_temp_c REAL, forecast_next_day_condition TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_hive_date ON hive_sensor_logs(hive_id, record_date);
        ''')
        c.commit(); c.close()
        return

    if DB.exists():
        try:
            DB.unlink()
        except PermissionError as e:
            raise PermissionError(
                'digital_hive.db занят. Закройте другие окна / DB Browser и удалите файл вручную.\n' + str(e)
            ) from e

    c = sqlite3.connect(str(DB))
    try:
        print(f'Инициализация БД из {sql_path.name} — может занять 30–90 секунд…',
              flush=True)
        c.execute('PRAGMA journal_mode = OFF;')
        c.execute('PRAGMA synchronous = OFF;')
        c.execute('PRAGMA cache_size = -64000;')
        c.executescript(sql_path.read_text(encoding='utf-8'))
        c.commit()
        print(f'БД создана: {DB}', flush=True)
    except Exception as e:
        c.close()
        if DB.exists():
            try:
                DB.unlink()
            except OSError:
                pass
        raise RuntimeError(f'Ошибка создания БД из {sql_path.name}:\n{e}') from e
    c.close()

    try:
        cc = sqlite3.connect(str(DB))
        cc.execute('PRAGMA journal_mode=WAL')
        cc.execute('PRAGMA synchronous=NORMAL')
        cc.close()
    except sqlite3.Error:
        pass


class Card(QFrame):
    def __init__(self, accent_color=None):
        super().__init__()
        self.setObjectName('card')
        if accent_color:
            self.setStyleSheet(
                f'#card {{ background: white; border: 1px solid {BORDER}; '
                f'border-top: 3px solid {accent_color}; border-radius: 14px; }}'
            )


class MetricCard(QFrame):
    def __init__(self, title, unit, kind='line'):
        super().__init__()
        self.setObjectName('card')
        self.unit = unit
        self.kind = kind
        self.points = []
        self.annot = None
        self.dot = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 10)
        lay.setSpacing(6)
        top = QHBoxLayout()
        t_lbl = QLabel(title)
        t_lbl.setStyleSheet(f'font-size: 13px; font-weight: 700; color: {INK};')
        top.addWidget(t_lbl)
        top.addStretch()
        self.time_lbl = QLabel('—')
        self.time_lbl.setStyleSheet(f'font-size: 11px; color: {MUTED};')
        top.addWidget(self.time_lbl)
        lay.addLayout(top)
        stats_row = QHBoxLayout()
        stats_row.setSpacing(18)
        avg_box = QVBoxLayout()
        avg_box.setSpacing(1)
        avg_cap = QLabel('В среднем')
        avg_cap.setStyleSheet(f'font-size: 11px; color: {MUTED};')
        self.avg_val = QLabel('—')
        self.avg_val.setStyleSheet(f'font-size: 20px; font-weight: 800; color: {INK};')
        avg_box.addWidget(avg_cap)
        avg_box.addWidget(self.avg_val)
        stats_row.addLayout(avg_box)
        diff_box = QVBoxLayout()
        diff_box.setSpacing(1)
        diff_cap = QLabel('За период')
        diff_cap.setStyleSheet(f'font-size: 11px; color: {MUTED};')
        self.diff_val = QLabel('—')
        self.diff_val.setStyleSheet(f'font-size: 17px; font-weight: 800; color: {MUTED};')
        diff_box.addWidget(diff_cap)
        diff_box.addWidget(self.diff_val)
        stats_row.addLayout(diff_box)
        stats_row.addStretch()
        lay.addLayout(stats_row)
        self.fig = Figure(figsize=(4, 2.0), dpi=96)
        self.fig.patch.set_facecolor('white')
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setMinimumHeight(165)
        self.canvas.mpl_connect('motion_notify_event', self.on_hover)
        lay.addWidget(self.canvas, 1)

    def render(self, labels, values):
        self.fig.clear()
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('white')
        self.points = []
        self.annot = None
        self.dot = None
        if not values:
            self.avg_val.setText('—')
            self.diff_val.setText('—')
            self.time_lbl.setText('Нет данных')
            self.ax.text(0.5, 0.5, 'Нет записей', ha='center', va='center', transform=self.ax.transAxes, color=MUTED, fontsize=9)
            self.ax.axis('off')
            self.fig.tight_layout(pad=0.4)
            self.canvas.draw_idle()
            return
        avg_v = sum(values) / len(values)
        diff_v = (values[-1] - values[0]) if len(values) > 1 else 0.0
        self.avg_val.setText(f'{avg_v:.1f} {self.unit}')
        last_lbl = labels[-1]
        self.time_lbl.setText(last_lbl[-5:] if len(last_lbl) > 10 else last_lbl)
        sign = '+' if diff_v > 0 else ''
        self.diff_val.setText(f'{sign}{diff_v:.1f} {self.unit}')
        if diff_v > 0.05:
            self.diff_val.setStyleSheet('font-size: 17px; font-weight: 800; color: #16A34A;')
        elif diff_v < -0.05:
            self.diff_val.setStyleSheet(f'font-size: 17px; font-weight: 800; color: {O};')
        else:
            self.diff_val.setStyleSheet(f'font-size: 17px; font-weight: 800; color: {MUTED};')
        xs = list(range(len(values)))
        self.points = list(zip(xs, values, labels))
        if self.kind == 'bar':
            colors = [P if (i == 0 or values[i] >= values[i - 1]) else O for i in xs]
            self.ax.bar(xs, values, color=colors, width=0.72, alpha=0.92)
            ymin, ymax = min(values), max(values)
            pad = max((ymax - ymin) * 0.15, 0.5)
            self.ax.set_ylim(max(0, ymin - pad), ymax + pad)
        else:
            self.ax.plot(xs, values, color=P, linewidth=2.0)
            self.ax.fill_between(xs, values, min(values), color=P, alpha=0.10)
        step = max(1, len(xs) // 4)
        self.ax.set_xticks(xs[::step])
        short = [lbl[-5:] if len(lbl) > 10 else (lbl if len(lbl) == 7 else lbl[5:]) for lbl in labels[::step]]
        self.ax.set_xticklabels(short, fontsize=7.5, color=MUTED)
        self.ax.grid(axis='y', linestyle='--', alpha=0.25, color='#CBD5E1')
        self.ax.spines[['top', 'right', 'left']].set_visible(False)
        self.ax.spines['bottom'].set_color(BORDER)
        self.ax.tick_params(axis='y', colors=MUTED, labelsize=7.5, length=0)
        self.ax.tick_params(axis='x', colors=MUTED, length=3)
        self.dot, = self.ax.plot([], [], marker='o', markersize=6, color=O, markeredgecolor='white', markeredgewidth=1.2, zorder=5)
        self.annot = self.ax.annotate('', xy=(0, 0), xytext=(0, 10), textcoords='offset points', ha='center', va='bottom', fontsize=8, fontweight='bold', color='white', bbox=dict(boxstyle='round,pad=0.35', fc=INK, ec=P, lw=1), zorder=6)
        self.annot.set_visible(False)
        self.fig.tight_layout(pad=0.5)
        self.canvas.draw_idle()

    def on_hover(self, event):
        if not self.points or self.annot is None:
            return
        if event.inaxes != self.ax or event.xdata is None:
            if self.annot.get_visible():
                self.annot.set_visible(False)
                self.dot.set_data([], [])
                self.canvas.draw_idle()
            return
        idx = max(0, min(len(self.points) - 1, int(round(event.xdata))))
        x, y, lbl = self.points[idx]
        n = len(self.points)
        ha, dx = ('left', 6) if idx < n * 0.2 else (('right', -6) if idx > n * 0.8 else ('center', 0))
        ymin, ymax = self.ax.get_ylim()
        span = max(ymax - ymin, 1e-6)
        va, dy = ('top', -12) if (y - ymin) / span > 0.68 else ('bottom', 10)
        self.annot.set_ha(ha)
        self.annot.set_va(va)
        self.annot.xytext = (dx, dy)
        self.annot.xy = (x, y)
        self.annot.set_text(f'{lbl}\n{y:.2f} {self.unit}')
        self.annot.set_visible(True)
        self.dot.set_data([x], [y])
        self.canvas.draw_idle()


class AIWorker(QObject):
    finished = Signal(str)
    error = Signal(str)
    def __init__(self, message, context, history):
        super().__init__()
        self.message, self.context, self.history = message, context, history
    def run(self):
        try:
            answer = get_ai_response(self.message, self.context, self.history)
            self.finished.emit(str(answer or 'Пустой ответ.'))
        except Exception as e:
            self.error.emit(f'{type(e).__name__}: {e}')


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        init_db()
        self.db = connect(DB)
        self.setWindowTitle('Цифровой улей — Мониторинг пасеки')
        self.resize(1460, 920)
        self.setMinimumSize(1160, 740)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.tick)
        self.chat_history = []
        self._ai_thread = None
        self._ai_worker = None
        self._ai_busy = False
        self._welcome = None
        self._typing_row = None
        self._last_bubble_role = None
        self.build()
        self.load_filters()
        self.chart()
        self.alerts()
        self._refresh_llm_status()

    def build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side = QFrame()
        side.setObjectName('sidebar')
        side.setFixedWidth(245)
        s = QVBoxLayout(side)
        s.setContentsMargins(20, 26, 20, 22)
        s.setSpacing(8)
        logo_row = QHBoxLayout()
        hex_badge = QLabel('⬡')
        hex_badge.setStyleSheet(f'background: {P_SOFT}; color: {P}; font-size: 22px; font-weight: 800; border-radius: 10px; padding: 6px 10px;')
        logo_row.addWidget(hex_badge)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        brand = QLabel('Цифровой улей')
        brand.setStyleSheet(f'font-size: 17px; font-weight: 800; color: {INK};')
        sub = QLabel('Телеметрия пасеки')
        sub.setStyleSheet(f'font-size: 12px; color: {MUTED};')
        title_box.addWidget(brand)
        title_box.addWidget(sub)
        logo_row.addLayout(title_box, 1)
        s.addLayout(logo_row)
        s.addSpacing(26)
        menu_label = QLabel('РАЗДЕЛЫ')
        menu_label.setObjectName('sectionLabel')
        s.addWidget(menu_label)
        self.nav = []
        for text, idx in [('✦   ИИ-консультант', 0), ('▥   Аналитика и графики', 1), ('◉   Журнал оповещений', 2)]:
            b = QPushButton(text)
            b.setObjectName('nav')
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, n=idx: self.page(n))
            s.addWidget(b)
            self.nav.append(b)
        s.addSpacing(16)
        data_label = QLabel('ДАННЫЕ')
        data_label.setObjectName('sectionLabel')
        s.addWidget(data_label)
        for text, slot in [('📂  Импорт базы', self.dialog_import_db), ('🔌  Загрузка по проводу', self.dialog_serial_import), ('📄  Импорт CSV / JSON', self.dialog_file_telemetry)]:
            b = QPushButton(text)
            b.setObjectName('nav')
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            s.addWidget(b)
        s.addStretch()
        db_pill = QFrame()
        db_pill.setStyleSheet(f'background: #F8F9FC; border: 1px solid {BORDER}; border-radius: 10px;')
        db_l = QHBoxLayout(db_pill)
        db_l.setContentsMargins(12, 10, 12, 10)
        self.db_dot = QLabel('●')
        self.db_dot.setStyleSheet('color: #16A34A; font-size: 10px;')
        self.db_text = QLabel('SQLite подключена')
        self.db_text.setStyleSheet(f'color: {MUTED}; font-size: 12px; font-weight: 600;')
        db_l.addWidget(self.db_dot)
        db_l.addWidget(self.db_text, 1)
        s.addWidget(db_pill)
        outer.addWidget(side)
        area = QWidget()
        v = QVBoxLayout(area)
        v.setContentsMargins(28, 22, 28, 22)
        v.setSpacing(14)
        head = QHBoxLayout()
        self.heading = QLabel('Аналитика и графики')
        self.heading.setObjectName('heading')
        head.addWidget(self.heading)
        head.addStretch()
        season_badge = QLabel('Сезон 2024–2026')
        season_badge.setStyleSheet(f'background: white; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 12px; color: {MUTED}; font-weight: 600; font-size: 12px;')
        head.addWidget(season_badge)
        v.addLayout(head)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.ai_page())
        self.stack.addWidget(self.analytics_page())
        self.stack.addWidget(self.alert_page())
        v.addWidget(self.stack, 1)
        outer.addWidget(area, 1)
        self.page(1)

    # ---------- ИИ-ЧАТ (НОВЫЙ ДИЗАЙН) ----------
    def ai_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)

        # --- Hero-карточка ---
        hero = Card(accent_color=P)
        h = QHBoxLayout(hero)
        h.setContentsMargins(24, 18, 24, 18)
        x = QVBoxLayout()
        x.setSpacing(4)
        tag = QLabel('ИИ · LM STUDIO')
        tag.setObjectName('eyebrow')
        x.addWidget(tag)
        title = QLabel('Консультант пчеловода')
        title.setStyleSheet(f'font-size: 22px; font-weight: 800; color: {INK};')
        x.addWidget(title)
        subtitle = QLabel('Задайте вопрос — модель проанализирует телеметрию выбранного улья')
        subtitle.setStyleSheet(f'font-size: 13px; color: {MUTED};')
        x.addWidget(subtitle)
        h.addLayout(x, 1)

        self.llm_status = QLabel('Проверка модели…')
        self.llm_status.setStyleSheet(
            f'background: {P_SOFT}; color: {P}; font-size: 12px; font-weight: 700; '
            f'padding: 6px 12px; border-radius: 8px;'
        )
        h.addWidget(self.llm_status, 0, alignment=Qt.AlignTop)

        clear_btn = QPushButton('Очистить')
        clear_btn.setObjectName('secondary')
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setMinimumHeight(34)
        clear_btn.clicked.connect(self.clear_chat)
        h.addWidget(clear_btn, 0, alignment=Qt.AlignTop)
        l.addWidget(hero)

        # --- Карточка чата ---
        c = Card()
        cl = QVBoxLayout(c)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_scroll.setObjectName('chatScroll')
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_scroll.setStyleSheet(
            'QScrollArea#chatScroll { background: #FBFBFE; border: 0; }'
            'QScrollArea#chatScroll QScrollBar:vertical {'
            '    background: transparent; width: 10px; margin: 6px 2px 6px 0; }'
            'QScrollArea#chatScroll QScrollBar::handle:vertical {'
            '    background: #D5D8E2; border-radius: 4px; min-height: 40px; }'
            'QScrollArea#chatScroll QScrollBar::handle:vertical:hover {'
            '    background: #B8BCC9; }'
            'QScrollArea#chatScroll QScrollBar::add-line:vertical,'
            'QScrollArea#chatScroll QScrollBar::sub-line:vertical { height: 0; }'
        )

        self.chat_container = QWidget()
        self.chat_container.setObjectName('chatContainer')
        self.chat_container.setStyleSheet('QWidget#chatContainer { background: #FBFBFE; }')
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(28, 24, 28, 24)
        self.chat_layout.setSpacing(4)
        self.chat_layout.addStretch(1)

        self._welcome = self._make_welcome_widget()
        self.chat_layout.insertWidget(0, self._welcome)

        self.chat_scroll.setWidget(self.chat_container)
        cl.addWidget(self.chat_scroll, 1)

        # --- Панель ввода ---
        input_bar = QFrame()
        input_bar.setObjectName('chatInputBar')
        input_bar.setStyleSheet(
            f'QFrame#chatInputBar {{ background: white; border-top: 1px solid {BORDER}; '
            f'border-bottom-left-radius: 14px; border-bottom-right-radius: 14px; }}'
        )
        ib = QHBoxLayout(input_bar)
        ib.setContentsMargins(18, 14, 18, 16)
        ib.setSpacing(12)

        self.chat_input = QLineEdit()
        self.chat_input.setObjectName('chatInput')
        self.chat_input.setPlaceholderText('Спросите что-нибудь о состоянии ульев…')
        self.chat_input.setMinimumHeight(48)
        self.chat_input.setStyleSheet(
            f'#chatInput {{ background: #F5F6FA; border: 1px solid {BORDER}; '
            f'border-radius: 14px; padding: 12px 18px; font-size: 14px; }}'
            f'#chatInput:focus {{ border-color: {P}; background: white; }}'
        )
        self.chat_input.returnPressed.connect(self.send_chat)
        ib.addWidget(self.chat_input, 1)

        self.send_btn = QPushButton('➤  Отправить')
        self.send_btn.setObjectName('primary')
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setMinimumHeight(48)
        self.send_btn.setMinimumWidth(140)
        self.send_btn.setStyleSheet(
            f'QPushButton#primary {{ background: {P}; color: white; border: 0; '
            f'border-radius: 14px; padding: 12px 20px; font-size: 14px; font-weight: 700; }}'
            f'QPushButton#primary:hover {{ background: {P_DARK}; }}'
            f'QPushButton#primary:disabled {{ background: #FDBA74; }}'
        )
        self.send_btn.clicked.connect(self.send_chat)
        ib.addWidget(self.send_btn)

        cl.addWidget(input_bar)
        l.addWidget(c, 1)
        return w

    def _make_welcome_widget(self):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 40, 0, 40)
        v.setSpacing(10)

        icon = QLabel('⬡')
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f'background: {P_SOFT}; color: {P}; font-size: 30px; font-weight: 800; '
            f'border-radius: 24px; padding: 10px;'
        )
        icon.setFixedSize(64, 64)
        v.addWidget(icon, 0, alignment=Qt.AlignHCenter)

        title = QLabel('Чем помочь?')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f'font-size: 20px; font-weight: 800; color: {INK};')
        v.addWidget(title)

        hint = QLabel(
            'Спросите о температуре, влажности, аномалиях или\n'
            'погоде — модель проанализирует выбранный улей.'
        )
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(f'font-size: 14px; color: {MUTED}; line-height: 1.4;')
        v.addWidget(hint)
        return box

    def clear_chat(self):
        self.chat_history.clear()
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._typing_row = None
        self._last_bubble_role = None
        self._welcome = self._make_welcome_widget()
        self.chat_layout.insertWidget(0, self._welcome)

    def _show_typing_indicator(self):
        try:
            self._hide_typing_indicator()
            row = QWidget()
            row.setObjectName('typingRow')
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 6, 0, 6)
            row_l.setSpacing(12)

            avatar = QLabel('⬡')
            avatar.setFixedSize(42, 42)
            avatar.setAlignment(Qt.AlignCenter)
            avatar.setStyleSheet(
                f'background: {P_SOFT}; color: {P}; border-radius: 21px; '
                f'font-size: 20px; font-weight: 800;'
            )

            bubble = QLabel('●  ●  ●')
            bubble.setStyleSheet(
                f'background: white; color: {P}; border: 1px solid {BORDER}; '
                f'border-radius: 18px; border-top-left-radius: 6px; '
                f'padding: 14px 20px; font-size: 15px; letter-spacing: 4px;'
            )

            row_l.addWidget(avatar, 0, alignment=Qt.AlignTop)
            row_l.addWidget(bubble, 0, alignment=Qt.AlignTop)
            row_l.addStretch(1)

            self._typing_row = row
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
            QTimer.singleShot(40, self._scroll_chat_bottom)
        except Exception as e:
            print('typing:', e, file=sys.stderr)

    def _hide_typing_indicator(self):
        try:
            if getattr(self, '_typing_row', None) is not None:
                self._typing_row.setParent(None)
                self._typing_row.deleteLater()
                self._typing_row = None
        except Exception:
            pass

    def _append_chat_bubble(self, role, text):
        try:
            text = str(text or '')
            if len(text) > 50000:
                text = text[:50000] + '\n\n… [обрезано]'

            if self._welcome is not None:
                self.chat_layout.removeWidget(self._welcome)
                self._welcome.deleteLater()
                self._welcome = None

            last_role = getattr(self, '_last_bubble_role', None)
            show_avatar = (last_role != role)
            self._last_bubble_role = role

            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 6 if show_avatar else 1, 0, 6)
            row_l.setSpacing(12)

            avatar = QLabel('⬡' if role != 'user' else 'Вы')
            avatar.setFixedSize(42, 42)
            avatar.setAlignment(Qt.AlignCenter)
            if role == 'user':
                avatar.setStyleSheet(
                    f'background: {P}; color: white; border-radius: 21px; '
                    f'font-size: 12px; font-weight: 800;'
                )
            else:
                avatar.setStyleSheet(
                    f'background: {P_SOFT}; color: {P}; border-radius: 21px; '
                    f'font-size: 20px; font-weight: 800;'
                )

            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(4)

            bubble = QLabel(text)
            bubble.setWordWrap(True)
            bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            bubble.setMaximumWidth(max(320, int(self.chat_scroll.width() * 0.72)))

            ts = QLabel()
            ts.setStyleSheet(f'font-size: 11px; color: {MUTED}; padding: 0 6px;')

            if role == 'user':
                bubble.setStyleSheet(
                    f'background: {P}; color: white; border-radius: 18px; '
                    f'border-top-right-radius: 6px; padding: 14px 20px; '
                    f'font-size: 15px; line-height: 1.4;'
                )
                ts.setText('Вы · только что')
                ts.setAlignment(Qt.AlignRight)
                col.addWidget(bubble, 0, alignment=Qt.AlignRight)
                col.addWidget(ts, 0, alignment=Qt.AlignRight)

                if show_avatar:
                    row_l.addStretch(1)
                    row_l.addLayout(col, 0)
                    row_l.addWidget(avatar, 0, alignment=Qt.AlignTop)
                else:
                    row_l.addStretch(1)
                    spacer = QWidget()
                    spacer.setFixedWidth(54)
                    row_l.addLayout(col, 0)
                    row_l.addWidget(spacer, 0, alignment=Qt.AlignTop)
            else:
                bubble.setStyleSheet(
                    f'background: white; color: {INK}; border: 1px solid {BORDER}; '
                    f'border-radius: 18px; border-top-left-radius: 6px; '
                    f'padding: 14px 20px; font-size: 15px; line-height: 1.4;'
                )
                ts.setText('⬡ Консультант · только что')
                ts.setAlignment(Qt.AlignLeft)
                col.addWidget(bubble, 0, alignment=Qt.AlignLeft)
                col.addWidget(ts, 0, alignment=Qt.AlignLeft)

                if show_avatar:
                    row_l.addWidget(avatar, 0, alignment=Qt.AlignTop)
                    row_l.addLayout(col, 0)
                    row_l.addStretch(1)
                else:
                    spacer = QWidget()
                    spacer.setFixedWidth(54)
                    row_l.addWidget(spacer, 0, alignment=Qt.AlignTop)
                    row_l.addLayout(col, 0)
                    row_l.addStretch(1)

            self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
            QTimer.singleShot(40, self._scroll_chat_bottom)
        except Exception as e:
            print('chat:', e, file=sys.stderr)

    def _scroll_chat_bottom(self):
        try:
            bar = self.chat_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        except Exception:
            pass

    def _current_ai_context(self):
        try:
            d_from, d_to = None, None
            if getattr(self, 'current_period_idx', 2) == 5:
                d_from = self.start.date().toString('yyyy-MM-dd')
                d_to = self.end.date().toString('yyyy-MM-dd')

            return build_context_from_db(
                self.db,
                apiary_id=self.apiary.currentData() if hasattr(self, 'apiary') else None,
                hive_id=self.hive.currentData() if hasattr(self, 'hive') else None,
                date_from=d_from,
                date_to=d_to,
                recent_days=3650,
                max_anomalies=50,
                max_telemetry_rows=120,
            )
        except Exception as e:
            return {'error': str(e), 'latest_telemetry': [], 'recent_anomalies': []}

    def send_chat(self):
        text = self.chat_input.text().strip()
        if not text or self._ai_busy:
            return
        self.chat_input.clear()
        self._append_chat_bubble('user', text)
        self.chat_history.append({'role': 'user', 'content': text})
        self._ai_busy = True
        self.send_btn.setEnabled(False)
        self.chat_input.setEnabled(False)
        self.llm_status.setText('●  Модель думает…')
        self.llm_status.setStyleSheet(
            f'background: {O_SOFT}; color: {O}; font-size: 12px; font-weight: 700; '
            f'padding: 6px 12px; border-radius: 8px;'
        )
        self._show_typing_indicator()

        context = self._current_ai_context()
        history = list(self.chat_history[:-1])
        thread = QThread(self)
        worker = AIWorker(text, context, history)
        worker.moveToThread(thread)
        self._ai_thread = thread
        self._ai_worker = worker
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_ai_answer)
        worker.error.connect(self._on_ai_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_ai_thread_finished)
        thread.start()

    def _on_ai_answer(self, answer):
        self._hide_typing_indicator()
        try:
            self._append_chat_bubble('assistant', str(answer))
            self.chat_history.append({'role': 'assistant', 'content': str(answer)})
        finally:
            self._restore_chat_controls()
            self._refresh_llm_status()

    def _on_ai_error(self, err):
        self._hide_typing_indicator()
        try:
            self._append_chat_bubble('assistant', f'⚠ Ошибка: {err}')
        finally:
            self._restore_chat_controls()
            self._refresh_llm_status()

    def _on_ai_thread_finished(self):
        self._ai_busy = False
        w, t = self._ai_worker, self._ai_thread
        self._ai_worker = self._ai_thread = None
        if w:
            w.deleteLater()
        if t:
            t.deleteLater()

    def _restore_chat_controls(self):
        self._ai_busy = False
        self.send_btn.setEnabled(True)
        self.chat_input.setEnabled(True)
        self.chat_input.setFocus()

    def _refresh_llm_status(self):
        try:
            ok, msg = check_llm_available()
            self.llm_status.setText(('● ' if ok else '○ ') + msg)
            if ok:
                self.llm_status.setStyleSheet(
                    'background: #ECFDF5; color: #16A34A; font-size: 12px; '
                    'font-weight: 700; padding: 6px 12px; border-radius: 8px;'
                )
            else:
                self.llm_status.setStyleSheet(
                    f'background: {O_SOFT}; color: {O}; font-size: 12px; '
                    f'font-weight: 700; padding: 6px 12px; border-radius: 8px;'
                )
        except Exception:
            pass

    # ---------- АНАЛИТИКА ----------
    def analytics_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(12)
        top_bar = Card()
        tb = QVBoxLayout(top_bar)
        tb.setContentsMargins(16, 12, 16, 12)
        tb.setSpacing(10)
        hive_row = QHBoxLayout()
        hive_row.setSpacing(8)
        self.apiary = QComboBox()
        self.apiary.setMinimumWidth(190)
        hive_row.addWidget(self.apiary)
        prev_btn = QPushButton('‹')
        prev_btn.setObjectName('secondary')
        prev_btn.setFixedWidth(36)
        prev_btn.setCursor(Qt.PointingHandCursor)
        prev_btn.clicked.connect(lambda: self.step_hive(-1))
        hive_row.addWidget(prev_btn)
        self.hive = QComboBox()
        self.hive.setMinimumWidth(175)
        self.hive.currentIndexChanged.connect(self.chart)
        hive_row.addWidget(self.hive)
        next_btn = QPushButton('›')
        next_btn.setObjectName('secondary')
        next_btn.setFixedWidth(36)
        next_btn.setCursor(Qt.PointingHandCursor)
        next_btn.clicked.connect(lambda: self.step_hive(1))
        hive_row.addWidget(next_btn)
        hive_row.addStretch()
        self.kpi_summary = QLabel('Аномалий за период: 0')
        self.kpi_summary.setStyleSheet(
            'background: %s; color: %s; font-weight: 700; padding: 6px 12px; border-radius: 8px; font-size: 12px;' % (O_SOFT, O)
        )
        hive_row.addWidget(self.kpi_summary)
        tb.addLayout(hive_row)
        period_row = QHBoxLayout()
        period_row.setSpacing(6)
        self.period_btns = []
        for idx, p_name in enumerate(['Сегодня', 'Вчера', 'Неделя', 'Месяц', 'Год', 'Период']):
            btn = QPushButton(p_name)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                'QPushButton { background: #F1F3F8; color: %s; border: 0; border-radius: 8px; padding: 6px 14px; font-weight: 600; }'
                'QPushButton:hover { background: #E6E9F2; }'
                'QPushButton:checked { background: %s; color: white; font-weight: 700; }' % (INK, P)
            )
            btn.clicked.connect(lambda _, i=idx: self.set_period(i))
            period_row.addWidget(btn)
            self.period_btns.append(btn)
        period_row.addSpacing(12)
        self.custom_box = QWidget()
        cb_l = QHBoxLayout(self.custom_box)
        cb_l.setContentsMargins(0, 0, 0, 0)
        cb_l.setSpacing(6)
        self.start = QDateEdit(QDate(2026, 12, 1))
        self.end = QDateEdit(QDate(2026, 12, 31))
        for z in (self.start, self.end):
            z.setCalendarPopup(True)
            z.setDisplayFormat('dd.MM.yyyy')
        apply_btn = QPushButton('Показать')
        apply_btn.setObjectName('primary')
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(self.chart)
        cb_l.addWidget(QLabel('с'))
        cb_l.addWidget(self.start)
        cb_l.addWidget(QLabel('по'))
        cb_l.addWidget(self.end)
        cb_l.addWidget(apply_btn)
        self.custom_box.setVisible(False)
        period_row.addWidget(self.custom_box)
        period_row.addStretch()
        tb.addLayout(period_row)
        l.addWidget(top_bar)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards = {
            'hive_health_index': MetricCard('Индекс здоровья', 'из 5', 'line'),
            'internal_temp_c': MetricCard('Температура', '°C', 'line'),
            'internal_humidity_pct': MetricCard('Влажность', '%', 'line'),
            'co2_ppm': MetricCard('Уровень CO₂', 'ppm', 'line'),
            'hive_weight_kg': MetricCard('Масса улья', 'кг', 'bar'),
            'sound_level_db': MetricCard('Звук в улье', 'дБ', 'line'),
        }
        grid.addWidget(self.cards['hive_health_index'], 0, 0)
        grid.addWidget(self.cards['internal_temp_c'], 0, 1)
        grid.addWidget(self.cards['internal_humidity_pct'], 0, 2)
        grid.addWidget(self.cards['co2_ppm'], 0, 3)
        grid.addWidget(self.cards['hive_weight_kg'], 1, 0, 1, 2)
        grid.addWidget(self.cards['sound_level_db'], 1, 2, 1, 2)
        l.addLayout(grid, 1)
        sim = Card()
        r = QHBoxLayout(sim)
        r.setContentsMargins(16, 10, 16, 10)
        r.setSpacing(12)
        r.addWidget(QLabel('Поиск аномалии:'))
        self.status = QLabel('Выберите дату старта')
        self.status.setObjectName('muted')
        r.addWidget(self.status, 1)
        self.testdate = QDateEdit(QDate(2024, 1, 1))
        self.testdate.setCalendarPopup(True)
        self.testdate.setDisplayFormat('dd.MM.yyyy')
        r.addWidget(self.testdate)
        self.run = QPushButton('▶  Запустить тест')
        self.run.setObjectName('primary')
        self.run.setCursor(Qt.PointingHandCursor)
        self.run.clicked.connect(self.start_test)
        r.addWidget(self.run)
        self.stop = QPushButton('Стоп')
        self.stop.setObjectName('secondary')
        self.stop.setCursor(Qt.PointingHandCursor)
        self.stop.clicked.connect(self.stop_test)
        self.stop.setEnabled(False)
        r.addWidget(self.stop)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(120)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        r.addWidget(self.progress)
        l.addWidget(sim)
        self.current_period_idx = 2
        self.period_btns[2].setChecked(True)
        return w

    def step_hive(self, delta):
        cnt = self.hive.count()
        if cnt <= 1:
            return
        self.hive.setCurrentIndex((self.hive.currentIndex() + delta) % cnt)

    def set_period(self, idx):
        self.current_period_idx = idx
        for i, b in enumerate(self.period_btns):
            b.setChecked(i == idx)
        self.custom_box.setVisible(idx == 5)
        if idx != 5:
            self.chart()

    def alert_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)
        row = QHBoxLayout()
        t = QLabel("События и тревоги с датчиков")
        t.setStyleSheet("font-size: 17px; font-weight: 700;")
        row.addWidget(t)
        row.addStretch()
        self.only = QComboBox()
        self.only.addItems(["Все записи", "Только аномалии"])
        self.only.currentIndexChanged.connect(self.alerts)
        row.addWidget(self.only)
        b = QPushButton("Обновить")
        b.setObjectName("secondary")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(self.alerts)
        row.addWidget(b)
        l.addLayout(row)
        c = Card()
        q = QVBoxLayout(c)
        q.setContentsMargins(12, 12, 12, 12)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Дата", "Пасека", "Улей", "Статус", "Телеметрия", "Описание события"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        for i, ww in enumerate([145, 160, 95, 115, 210]):
            self.table.setColumnWidth(i, ww)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        q.addWidget(self.table)
        l.addWidget(c, 1)
        return w

    def page(self, n):
        self.stack.setCurrentIndex(n)
        self.heading.setText(["ИИ-консультант", "Аналитика и графики", "Журнал оповещений"][n])
        for i, b in enumerate(self.nav):
            b.setChecked(i == n)
        if n == 2:
            self.alerts()
        if n == 0:
            self._refresh_llm_status()

    def load_filters(self):
        self.apiary.blockSignals(True)
        self.apiary.clear()
        for r in self.db.execute("SELECT apiary_id, name FROM apiaries ORDER BY apiary_id"):
            self.apiary.addItem(r["name"], r["apiary_id"])
        self.apiary.blockSignals(False)
        try:
            hive_count = self.db.execute('SELECT COUNT(*) FROM hives').fetchone()[0]
            log_count = self.db.execute('SELECT COUNT(*) FROM hive_sensor_logs').fetchone()[0]
            if hasattr(self, 'db_text'):
                self.db_text.setText(f'SQLite · {hive_count} ульев · {log_count} записей')
        except sqlite3.Error:
            pass
        try:
            self.apiary.currentIndexChanged.disconnect(self.load_hives)
        except (TypeError, RuntimeError):
            pass
        self.apiary.currentIndexChanged.connect(self.load_hives)
        self.load_hives()

    def load_hives(self):
        self.hive.clear()
        self.hive.addItem("Все ульи пасеки", None)
        apiary_id = self.apiary.currentData()
        if apiary_id is None:
            return
        for r in self.db.execute("SELECT hive_id, hive_code FROM hives WHERE apiary_id = ? ORDER BY hive_id", (apiary_id,)):
            self.hive.addItem(r["hive_code"], r["hive_id"])
        if self.hive.count() > 1:
            self.hive.setCurrentIndex(1)

    def where(self):
        if self.hive.currentData() is not None:
            return "l.hive_id = ?", [self.hive.currentData()]
        return "h.apiary_id = ?", [self.apiary.currentData()]

    def _time_column(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(hive_sensor_logs)").fetchall()}
        return "record_datetime" if "record_datetime" in cols else "record_date"

    def _has_column(self, col: str) -> bool:
        try:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(hive_sensor_logs)").fetchall()}
            return col in cols
        except sqlite3.Error:
            return False

    def chart(self):
        if not hasattr(self, 'cards'):
            return
        tcol = self._time_column()
        max_row = self.db.execute(f'SELECT MAX({tcol}) FROM hive_sensor_logs').fetchone()
        max_dt_str = max_row[0] if max_row and max_row[0] else date.today().isoformat()
        anchor_date = date.fromisoformat(str(max_dt_str)[:10])
        idx = getattr(self, 'current_period_idx', 2)
        mode = 'day'
        if idx == 0:
            d_from, d_to, mode = anchor_date, anchor_date, 'hour'
        elif idx == 1:
            d_from = d_to = anchor_date - timedelta(days=1)
            mode = 'hour'
        elif idx == 2:
            d_from, d_to = anchor_date - timedelta(days=6), anchor_date
        elif idx == 3:
            d_from, d_to = anchor_date - timedelta(days=29), anchor_date
        elif idx == 4:
            d_from, d_to, mode = anchor_date - timedelta(days=364), anchor_date, 'month'
        else:
            d_from = date.fromisoformat(self.start.date().toString('yyyy-MM-dd'))
            d_to = date.fromisoformat(self.end.date().toString('yyyy-MM-dd'))
            if d_from > d_to:
                QMessageBox.warning(self, 'Период', 'Начальная дата позже конечной.')
                return
            span = (d_to - d_from).days
            if span <= 2:
                mode = 'hour'
            elif span >= 364:
                mode = 'month'
        if tcol == 'record_datetime':
            a, b = d_from.isoformat() + ' 00:00:00', d_to.isoformat() + ' 23:59:59'
            if mode == 'hour':
                grp = f'substr(l.{tcol}, 1, 13)'
            elif mode == 'month':
                grp = f'substr(l.{tcol}, 1, 7)'
            else:
                grp = f'substr(l.{tcol}, 1, 10)'
        else:
            a, b = d_from.isoformat(), d_to.isoformat()
            grp = f'substr(l.{tcol}, 1, 7)' if mode == 'month' else f'l.{tcol}'
            if mode == 'hour':
                mode = 'day'
        cond, args = self.where()

        agg_fields = [
            ("bee_activity_index", "bee_activity_index"),
            ("internal_temp_c", "internal_temp_c"),
            ("internal_humidity_pct", "internal_humidity_pct"),
            ("co2_ppm", "co2_ppm"),
            ("hive_weight_kg", "hive_weight_kg"),
            ("sound_level_db", "sound_level_db"),
            ("hive_health_index", "hive_health_index"),
        ]
        select_aggs = []
        for col, alias in agg_fields:
            if self._has_column(col):
                select_aggs.append(f'AVG(l.{col}) AS {alias}')
        select_aggs.append('SUM(l.anomaly_flag) AS anom')
        if self._has_column("pesticide_risk"):
            select_aggs.append('SUM(l.pesticide_risk) AS pest_hours')

        rows = self.db.execute(
            f'SELECT {grp} AS bucket, {", ".join(select_aggs)} '
            f'FROM hive_sensor_logs l JOIN hives h ON h.hive_id = l.hive_id '
            f'WHERE {cond} AND l.{tcol} BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket',
            args + [a, b],
        ).fetchall()
        labels = [(r['bucket'] + ':00') if mode == 'hour' else r['bucket'] for r in rows]
        anom_total = sum(r['anom'] or 0 for r in rows)
        pest_total = sum((r['pest_hours'] or 0) for r in rows) if 'pest_hours' in rows[0].keys() else 0
        self.kpi_summary.setText(
            f'Аномалий за период: {anom_total}' + (f' · риск пестицидов: {pest_total} ч' if pest_total else '')
        )
        for col, card in self.cards.items():
            if col not in rows[0].keys():
                card.render([], [])
                continue
            vals = [float(r[col]) for r in rows if r[col] is not None]
            sub = [labels[i] for i, r in enumerate(rows) if r[col] is not None]
            card.render(sub, vals)

    def alerts(self):
        if not hasattr(self, 'table'):
            return
        tcol = self._time_column()
        has_pest = self._has_column("pesticide_risk")
        has_health = self._has_column("hive_health_index")
        extra = []
        if has_pest:
            extra.append("l.pesticide_risk")
        if has_health:
            extra.append("l.hive_health_index")
        extra_sql = (", " + ", ".join(extra)) if extra else ""
        sql = (
            f'SELECT l.{tcol} AS ts, a.name AS apiary, h.hive_code, l.anomaly_flag, l.anomaly_description, '
            f'l.internal_temp_c, l.internal_humidity_pct, l.co2_ppm{extra_sql} FROM hive_sensor_logs l '
            f'JOIN hives h ON h.hive_id = l.hive_id JOIN apiaries a ON a.apiary_id = h.apiary_id'
        )
        if self.only.currentIndex() == 1:
            sql += ' WHERE l.anomaly_flag = 1'
        sql += f' ORDER BY l.{tcol} DESC, l.log_id DESC LIMIT 500'
        try:
            rows = self.db.execute(sql).fetchall()
        except sqlite3.Error as err:
            QMessageBox.critical(self, 'БД', str(err))
            return
        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setRowHeight(i, 36)
            desc = str(r['anomaly_description'] or 'Норма')
            is_anom = bool(r['anomaly_flag'])
            if 'КРИТИЧЕСКАЯ' in desc.upper():
                level = 'Критическая'
            elif is_anom:
                level = 'Тревога'
            else:
                level = 'Норма'
            t_val = f"{r['internal_temp_c']} °C" if r['internal_temp_c'] is not None else '—'
            h_val = f"{r['internal_humidity_pct']}%" if r['internal_humidity_pct'] is not None else '—'
            c_val = f"CO₂ {r['co2_ppm']}" if r['co2_ppm'] is not None else '—'
            keys = r.keys()
            if 'hive_health_index' in keys and r['hive_health_index'] is not None:
                t_val += f" · ИЗ {r['hive_health_index']}"
            if 'pesticide_risk' in keys and r['pesticide_risk']:
                c_val += " · ПЕСТИЦИДЫ"
            vals = [str(r['ts'] or '—'), str(r['apiary'] or '—'), str(r['hive_code'] or '—'), level, f'{t_val}  ·  {h_val}  ·  {c_val}', desc]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 3:
                    font = it.font()
                    font.setBold(True)
                    it.setFont(font)
                    if level != 'Норма':
                        it.setForeground(QColor(O))
                        it.setBackground(QColor(O_SOFT))
                    else:
                        it.setForeground(QColor('#16A34A'))
                elif j == 4 and 'pesticide_risk' in keys and r['pesticide_risk']:
                    it.setForeground(QColor(O))
                self.table.setItem(i, j, it)
        self.table.setUpdatesEnabled(True)

    def start_test(self):
        if self.timer.isActive():
            return
        self.day = date.fromisoformat(self.testdate.date().toString('yyyy-MM-dd'))
        if self.day > date(2026, 12, 31):
            QMessageBox.warning(self, 'Дата', 'Не позднее 31.12.2026')
            return
        self.run.setEnabled(False)
        self.stop.setEnabled(True)
        self.progress.setValue(0)
        self.timer.start()
        self.status.setText('Симуляция запущена…')

    def tick(self):
        if self.day > date(2026, 12, 31):
            self.finish('До конца периода аномалий не обнаружено.')
            return
        day_str = self.day.isoformat()
        tcol = self._time_column()
        if tcol == 'record_datetime':
            day_start, day_end = day_str + ' 00:00:00', day_str + ' 23:59:59'
        else:
            day_start = day_end = day_str
        cond, args = self.where()
        row = self.db.execute(
            f'SELECT l.*, h.hive_code, a.name AS apiary FROM hive_sensor_logs l '
            f'JOIN hives h ON h.hive_id = l.hive_id JOIN apiaries a ON a.apiary_id = h.apiary_id '
            f'WHERE {cond} AND l.{tcol} BETWEEN ? AND ? AND l.anomaly_flag = 1 ORDER BY l.log_id LIMIT 1',
            args + [day_start, day_end],
        ).fetchone()
        self.status.setText(f'Анализ за {self.day.strftime("%d.%m.%Y")}…')
        total = max(1, (date(2026, 12, 31) - date(2024, 1, 1)).days)
        self.progress.setValue(min(100, int(max(0, (self.day - date(2024, 1, 1)).days) / total * 100)))
        if row:
            self.timer.stop()
            self.finish(None)
            self.only.blockSignals(True)
            self.only.setCurrentIndex(1)
            self.only.blockSignals(False)
            self.page(2)
            self.start.setDate(QDate.fromString(day_str, 'yyyy-MM-dd'))
            self.end.setDate(QDate.fromString(day_str, 'yyyy-MM-dd'))
            self.set_period(5)
            self.chart()
            extra = ''
            keys = row.keys()
            if 'hive_health_index' in keys and row['hive_health_index'] is not None:
                extra += f"\nИндекс здоровья: {row['hive_health_index']} / 5"
            if 'pesticide_risk' in keys and row['pesticide_risk']:
                extra += "\nРиск пестицидов: ДА"
            msg = (
                f"Дата: {self.day.strftime('%d.%m.%Y')}\nПасека: {row['apiary']}\nУлей: {row['hive_code']}\n\n"
                f"{row['anomaly_description'] or 'Отклонение'}\n\nВес: {row['hive_weight_kg']} кг\n"
                f"Т: {row['internal_temp_c']} °C\nВлажн.: {row['internal_humidity_pct']}%\nCO₂: {row['co2_ppm']}"
                f"{extra}"
            )
            QTimer.singleShot(50, lambda: QMessageBox.warning(self, 'Аномалия', msg))
            return
        self.day += timedelta(days=1)

    def finish(self, msg):
        self.timer.stop()
        self.run.setEnabled(True)
        self.stop.setEnabled(False)
        self.status.setText(msg or 'Тест остановлен на найденной аномалии.')
        if msg:
            QMessageBox.information(self, 'Готово', msg)

    def stop_test(self):
        if self.timer.isActive():
            self.finish('Остановлено пользователем.')

    def _reload_after_data_change(self):
        try:
            self.db.close()
        except Exception:
            pass
        self.db = connect(DB)
        try:
            clear_caches()
        except Exception:
            pass
        self.load_filters()
        self.chart()
        self.alerts()
        db_counts = self.db.execute('SELECT COUNT(*) FROM hives').fetchone()[0]
        log_counts = self.db.execute('SELECT COUNT(*) FROM hive_sensor_logs').fetchone()[0]
        self.db_text.setText(f'SQLite · {db_counts} ульев · {log_counts} записей')
        if hasattr(self, 'chat_layout'):
            self.clear_chat()
            self._append_chat_bubble('assistant',
                                     'База данных обновлена. Я готов анализировать новые показатели ульев.')

    def dialog_import_db(self):
        path, _ = QFileDialog.getOpenFileName(self, 'База или SQL', str(ROOT), 'SQLite / SQL (*.db *.sqlite *.sqlite3 *.sql);;Все (*.*)')
        if not path:
            return
        pth = Path(path)
        if pth.resolve() == DB.resolve():
            QMessageBox.information(self, 'Импорт', 'Этот файл уже является активной базой данных.')
            return
        dlg = QDialog(self)
        dlg.setWindowTitle('Импорт базы')
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f'Файл: {pth.name}'))
        mode_replace = QRadioButton('Заменить текущую базу')
        mode_merge = QRadioButton('Объединить (merge)')
        mode_replace.setChecked(True)
        if pth.suffix.lower() == '.sql':
            mode_merge.setEnabled(False)
        lay.addWidget(mode_replace)
        lay.addWidget(mode_merge)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        lay.addWidget(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            self.db.close()
        except Exception:
            pass
        try:
            if pth.suffix.lower() == '.sql':
                msg = import_sql_script(pth, DB)
            else:
                ok, vmsg = validate_db(pth)
                if not ok:
                    QMessageBox.critical(self, 'Импорт', vmsg)
                    self.db = connect(DB)
                    return
                if mode_replace.isChecked() or not DB.exists():
                    msg = import_database(pth, DB, mode='replace')
                else:
                    msg = import_database(pth, DB, mode='merge')
            self._reload_after_data_change()
            QMessageBox.information(self, 'Импорт', msg)
        except Exception as e:
            QMessageBox.critical(self, 'Импорт', str(e))
            try:
                self.db = connect(DB)
            except Exception:
                pass

    def dialog_file_telemetry(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Телеметрия', str(ROOT), 'Данные (*.csv *.txt *.json *.jsonl);;Все (*.*)')
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            QMessageBox.critical(self, 'Импорт', str(e))
            return
        rows = []
        first = raw.split('\n', 1)[0] if raw else ''
        if any(h in first for h in ('hive_', 'record_date', 'record_datetime', 'internal_temp')):
            rows = parse_csv_block(raw)
        else:
            for line in raw.splitlines():
                item = parse_sensor_line(line)
                if item:
                    rows.append(item)
        if not rows:
            QMessageBox.warning(self, 'Импорт', 'Не удалось распознать записи.')
            return
        inserted, skipped, errors = insert_sensor_rows(self.db, rows)
        self._reload_after_data_change()
        detail = f'Добавлено: {inserted}\nПропущено: {skipped}'
        if errors:
            detail += '\n\n' + '\n'.join(errors[:12])
        QMessageBox.information(self, 'Импорт телеметрии', detail)

    def dialog_serial_import(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('Загрузка по проводу (COM / USB)')
        dlg.setMinimumWidth(480)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        port_box = QComboBox()
        ports = list_serial_ports()
        if ports:
            port_box.addItems(ports)
        else:
            port_box.addItem('COM3')
            port_box.setEditable(True)
        form.addRow('Порт', port_box)
        baud_box = QComboBox()
        baud_box.addItems(['9600', '19200', '38400', '57600', '115200', '230400'])
        baud_box.setCurrentText('115200')
        form.addRow('Скорость', baud_box)
        duration = QSpinBox()
        duration.setRange(2, 120)
        duration.setValue(8)
        duration.setSuffix(' сек')
        form.addRow('Время чтения', duration)
        lay.addLayout(form)
        log = QPlainTextEdit()
        log.setReadOnly(True)
        log.setMinimumHeight(160)
        lay.addWidget(log)
        buttons = QDialogButtonBox()
        btn_read = buttons.addButton('Читать с порта', QDialogButtonBox.ActionRole)
        buttons.addButton('Записать в БД', QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        lay.addWidget(buttons)
        collected = []

        def do_read():
            port = port_box.currentText().strip()
            try:
                baud = int(baud_box.currentText())
            except ValueError:
                baud = 115200
            log.appendPlainText(f'— Чтение {port} @ {baud}…')
            QApplication.processEvents()
            try:
                lines = read_serial_lines(port, baud, duration_sec=float(duration.value()), on_line=lambda ln: log.appendPlainText(ln))
                collected.clear()
                collected.extend(lines)
                log.appendPlainText(f'— Строк: {len(lines)}')
            except Exception as e:
                log.appendPlainText(f'Ошибка: {e}')
                QMessageBox.critical(dlg, 'COM', str(e))

        def do_apply():
            rows = [x for x in (parse_sensor_line(ln) for ln in collected) if x]
            if not rows:
                QMessageBox.warning(dlg, 'Данные', 'Нет распознанных записей.')
                return
            inserted, skipped, errors = insert_sensor_rows(self.db, rows)
            self._reload_after_data_change()
            msg = f'Добавлено: {inserted}, пропущено: {skipped}'
            if errors:
                msg += '\n' + '\n'.join(errors[:8])
            QMessageBox.information(dlg, 'БД', msg)
            dlg.accept()

        btn_read.clicked.connect(do_read)
        buttons.accepted.connect(do_apply)
        buttons.rejected.connect(dlg.reject)
        dlg.exec()

    def closeEvent(self, e):
        self.timer.stop()
        if self._ai_thread is not None and self._ai_thread.isRunning():
            self._ai_thread.quit()
            self._ai_thread.wait(2000)
        self.db.close()
        e.accept()


def apply_style(app):
    app.setStyleSheet(
        "QWidget { font-family: 'Segoe UI', sans-serif; font-size: 13px; color: " + INK + "; }"
        "QMainWindow { background: " + BG + "; }"
        "#sidebar { background: white; border-right: 1px solid " + BORDER + "; }"
        "#muted { color: " + MUTED + "; }"
        "#heading { font-size: 24px; font-weight: 800; color: " + INK + "; }"
        "#sectionLabel { font-size: 11px; font-weight: 700; letter-spacing: 0.8px; color: " + MUTED + "; padding-left: 12px; margin-bottom: 4px; }"
        "#eyebrow { font-size: 11px; font-weight: 800; letter-spacing: 0.8px; color: " + P + "; }"
        "#card { background: white; border: 1px solid " + BORDER + "; border-radius: 14px; }"
        "#nav { text-align: left; padding: 12px 14px; border: 0; border-radius: 10px; background: transparent; color: #525866; font-weight: 600; }"
        "#nav:hover { background: #FFF7F5; color: " + P + "; }"
        "#nav:checked { background: " + P_SOFT + "; color: " + P + "; font-weight: 700; }"
        "#primary { background: " + P + "; color: white; border: 0; border-radius: 8px; padding: 8px 16px; font-weight: 700; }"
        "#primary:hover { background: " + P_DARK + "; }"
        "#primary:disabled { background: #FDBA74; }"
        "#secondary { background: #F1F3F8; color: " + INK + "; border: 1px solid " + BORDER + "; border-radius: 8px; padding: 8px 14px; font-weight: 600; }"
        "#secondary:hover { background: #E6E9F2; }"
        "#secondary:disabled { color: #A0A5B5; }"
        "QComboBox, QDateEdit { background: #FAFAFC; border: 1px solid " + BORDER + "; border-radius: 8px; padding: 7px 10px; min-width: 115px; }"
        "QComboBox:hover, QDateEdit:hover { border-color: " + P + "; }"
        "QProgressBar { border: 0; background: #E5E7EB; border-radius: 3px; }"
        "QProgressBar::chunk { background: " + O + "; border-radius: 3px; }"
        "QTableWidget { background: white; border: 0; alternate-background-color: #FAFBFD; selection-background-color: " + P_SOFT + "; }"
        "QHeaderView::section { background: #F6F7FB; border: 0; border-bottom: 1px solid " + BORDER + "; padding: 10px; font-weight: 700; color: #525866; }"
        "#chatScroll { background: #FBFBFE; border: 0; }"
        "#chatContainer { background: #FBFBFE; }"
        "#chatInputBar { background: white; border-top: 1px solid " + BORDER + "; }"
        "#chatInput { background: #F5F6FA; border: 1px solid " + BORDER + "; border-radius: 14px; padding: 12px 18px; font-size: 14px; }"
        "#chatInput:focus { border-color: " + P + "; background: white; }"
    )


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont('Segoe UI', 10))
    apply_style(app)
    try:
        w = Window()
    except Exception as e:
        err_text = traceback.format_exc()
        print(err_text, file=sys.stderr, flush=True)
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle('Ошибка запуска')
        box.setText(str(e))
        box.setDetailedText(err_text)
        box.exec()
        return 1
    w.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
