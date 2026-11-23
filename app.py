from __future__ import annotations

import faulthandler
faulthandler.enable()

import sys
import sqlite3
import traceback
from pathlib import Path
from datetime import date, timedelta

from PySide6.QtCore import Qt, QDate, QTimer, QThread, Signal, QObject
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QStackedWidget, QComboBox, QDateEdit,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QMessageBox, QLineEdit, QScrollArea, QFileDialog, QDialog, QDialogButtonBox,
    QFormLayout, QRadioButton, QSpinBox, QPlainTextEdit
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ai_connector import build_context_from_db, get_ai_response, check_llm_available
from data_io import (
    import_database, import_sql_script, validate_db, list_serial_ports,
    read_serial_lines, parse_sensor_line, parse_csv_block, insert_sensor_rows,
)

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'digital_hive.db'
SQL_CANDIDATES = [ROOT / 'smart_apiary_100_hives_hourly.sql', ROOT / 'pensil.sql']

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
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
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
        # Запуск без демо-SQL: пользователь сможет импортировать свою базу из интерфейса.
        c = sqlite3.connect(str(DB))
        c.executescript('''
            CREATE TABLE IF NOT EXISTS apiaries (apiary_id INTEGER PRIMARY KEY, name TEXT NOT NULL, district TEXT);
            CREATE TABLE IF NOT EXISTS hives (hive_id INTEGER PRIMARY KEY, apiary_id INTEGER NOT NULL, hive_code TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hive_sensor_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT, hive_id INTEGER NOT NULL,
                record_date TEXT NOT NULL, record_datetime TEXT,
                internal_temp_c REAL DEFAULT 0, internal_humidity_pct REAL DEFAULT 0,
                co2_ppm REAL DEFAULT 0, hive_weight_kg REAL DEFAULT 0,
                daily_weight_delta_kg REAL DEFAULT 0, lid_opened_count INTEGER DEFAULT 0,
                lid_event_cause TEXT DEFAULT 'Нет', is_tipped_over INTEGER DEFAULT 0,
                tipping_cause TEXT DEFAULT 'Нет', sound_level_db REAL DEFAULT 0,
                bee_activity_index REAL DEFAULT 0, estimated_flights_count INTEGER DEFAULT 0,
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
        c.execute('PRAGMA journal_mode = OFF;')
        c.execute('PRAGMA synchronous = OFF;')
        c.execute('PRAGMA cache_size = -64000;')
        c.executescript(sql_path.read_text(encoding='utf-8'))
        c.commit()
    except Exception as e:
        c.close()
        if DB.exists():
            try:
                DB.unlink()
            except OSError:
                pass
        raise RuntimeError(f'Ошибка создания БД из {sql_path.name}:\n{e}') from e
    c.close()


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

    def ai_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)
        hero = Card(accent_color=P)
        h = QHBoxLayout(hero)
        h.setContentsMargins(22, 16, 22, 16)
        x = QVBoxLayout()
        x.setSpacing(2)
        tag = QLabel('ИИ · LM STUDIO')
        tag.setObjectName('eyebrow')
        x.addWidget(tag)
        title = QLabel('Консультант пчеловода')
        title.setStyleSheet(f'font-size: 20px; font-weight: 800; color: {INK};')
        x.addWidget(title)
        h.addLayout(x, 1)
        self.llm_status = QLabel('Проверка модели…')
        self.llm_status.setStyleSheet(f'color: {MUTED}; font-size: 12px; font-weight: 600;')
        h.addWidget(self.llm_status)
        clear_btn = QPushButton('Очистить')
        clear_btn.setObjectName('secondary')
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.clicked.connect(self.clear_chat)
        h.addWidget(clear_btn)
        l.addWidget(hero)
        c = Card()
        cl = QVBoxLayout(c)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_scroll.setObjectName('chatScroll')
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_container = QWidget()
        self.chat_container.setObjectName('chatContainer')
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(18, 16, 18, 16)
        self.chat_layout.setSpacing(12)
        self.chat_layout.addStretch(1)
        self._welcome = QLabel('⬡  Задайте вопрос о состоянии ульев, аномалиях или погоде.\nКонтекст — пасека / улей с вкладки «Аналитика».')
        self._welcome.setWordWrap(True)
        self._welcome.setAlignment(Qt.AlignCenter)
        self._welcome.setStyleSheet(f'color: {MUTED}; font-size: 13px; padding: 40px 24px;')
        self.chat_layout.insertWidget(0, self._welcome)
        self.chat_scroll.setWidget(self.chat_container)
        cl.addWidget(self.chat_scroll, 1)
        input_bar = QFrame()
        input_bar.setObjectName('chatInputBar')
        ib = QHBoxLayout(input_bar)
        ib.setContentsMargins(14, 12, 14, 14)
        ib.setSpacing(10)
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName('chatInput')
        self.chat_input.setPlaceholderText('Напишите сообщение…  Enter — отправить')
        self.chat_input.setMinimumHeight(42)
        self.chat_input.returnPressed.connect(self.send_chat)
        ib.addWidget(self.chat_input, 1)
        self.send_btn = QPushButton('➤  Отправить')
        self.send_btn.setObjectName('primary')
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setMinimumHeight(42)
        self.send_btn.clicked.connect(self.send_chat)
        ib.addWidget(self.send_btn)
        cl.addWidget(input_bar)
        l.addWidget(c, 1)
        return w

    def clear_chat(self):
        self.chat_history.clear()
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._welcome = QLabel('⬡  Задайте вопрос о состоянии ульев, аномалиях или погоде.')
        self._welcome.setWordWrap(True)
        self._welcome.setAlignment(Qt.AlignCenter)
        self._welcome.setStyleSheet(f'color: {MUTED}; font-size: 13px; padding: 40px 24px;')
        self.chat_layout.insertWidget(0, self._welcome)

    def _append_chat_bubble(self, role, text):
        try:
            text = str(text or '')
            if len(text) > 50000:
                text = text[:50000] + '\n\n… [обрезано]'
            if self._welcome is not None:
                self.chat_layout.removeWidget(self._welcome)
                self._welcome.deleteLater()
                self._welcome = None
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(10)
            avatar = QLabel('⬡' if role != 'user' else 'Вы')
            avatar.setFixedSize(36, 36)
            avatar.setAlignment(Qt.AlignCenter)
            if role == 'user':
                avatar.setStyleSheet(f'background: {P}; color: white; border-radius: 18px; font-size: 11px; font-weight: 700;')
            else:
                avatar.setStyleSheet(f'background: {P_SOFT}; color: {P}; border-radius: 18px; font-size: 16px; font-weight: 800;')
            bubble = QLabel(text)
            bubble.setWordWrap(True)
            bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bubble.setMaximumWidth(620)
            if role == 'user':
                bubble.setStyleSheet(f'background: {P}; color: white; border-radius: 16px; border-top-right-radius: 4px; padding: 12px 16px; font-size: 13px;')
                row_l.addStretch(1)
                row_l.addWidget(bubble, 0, alignment=Qt.AlignRight | Qt.AlignTop)
                row_l.addWidget(avatar, 0, alignment=Qt.AlignTop)
            else:
                bubble.setStyleSheet(f'background: white; color: {INK}; border: 1px solid {BORDER}; border-radius: 16px; border-top-left-radius: 4px; padding: 12px 16px; font-size: 13px;')
                row_l.addWidget(avatar, 0, alignment=Qt.AlignTop)
                row_l.addWidget(bubble, 0, alignment=Qt.AlignLeft | Qt.AlignTop)
                row_l.addStretch(1)
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
            QTimer.singleShot(30, self._scroll_chat_bottom)
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
            # Если на вкладке аналитики выбран ручной период (индекс 5), передаём его ИИ
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
        self.llm_status.setText('Модель думает…')
        self.llm_status.setStyleSheet(f'color: {O}; font-size: 12px; font-weight: 600;')
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
        try:
            self._append_chat_bubble('assistant', str(answer))
            self.chat_history.append({'role': 'assistant', 'content': str(answer)})
        finally:
            self._restore_chat_controls()
            self._refresh_llm_status()

    def _on_ai_error(self, err):
        try:
            self._append_chat_bubble('assistant', f'Ошибка: {err}')
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
            self.llm_status.setStyleSheet(f'color: {"#16A34A" if ok else O}; font-size: 12px; font-weight: 600;')
        except Exception:
            pass

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
            'bee_activity_index': MetricCard('Активность пчёл', 'баллов', 'bar'),
            'internal_temp_c': MetricCard('Температура', '°C', 'line'),
            'internal_humidity_pct': MetricCard('Влажность', '%', 'line'),
            'co2_ppm': MetricCard('Уровень CO₂', 'ppm', 'line'),
            'hive_weight_kg': MetricCard('Масса улья', 'кг', 'bar'),
            'sound_level_db': MetricCard('Звук в улье', 'дБ', 'line'),
        }
        grid.addWidget(self.cards['bee_activity_index'], 0, 0)
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
        rows = self.db.execute(
            f'SELECT {grp} AS bucket, AVG(l.bee_activity_index) AS bee_activity_index, '
            f'AVG(l.internal_temp_c) AS internal_temp_c, AVG(l.internal_humidity_pct) AS internal_humidity_pct, '
            f'AVG(l.co2_ppm) AS co2_ppm, AVG(l.hive_weight_kg) AS hive_weight_kg, '
            f'AVG(l.sound_level_db) AS sound_level_db, SUM(l.anomaly_flag) AS anom '
            f'FROM hive_sensor_logs l JOIN hives h ON h.hive_id = l.hive_id '
            f'WHERE {cond} AND l.{tcol} BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket',
            args + [a, b],
        ).fetchall()
        labels = [(r['bucket'] + ':00') if mode == 'hour' else r['bucket'] for r in rows]
        self.kpi_summary.setText(f'Аномалий за период: {sum(r["anom"] or 0 for r in rows)}')
        for col, card in self.cards.items():
            vals = [float(r[col]) for r in rows if r[col] is not None]
            sub = [labels[i] for i, r in enumerate(rows) if r[col] is not None]
            card.render(sub, vals)

    def alerts(self):
        if not hasattr(self, 'table'):
            return
        tcol = self._time_column()
        sql = (
            f'SELECT l.{tcol} AS ts, a.name AS apiary, h.hive_code, l.anomaly_flag, l.anomaly_description, '
            f'l.internal_temp_c, l.internal_humidity_pct, l.co2_ppm FROM hive_sensor_logs l '
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
            msg = (
                f"Дата: {self.day.strftime('%d.%m.%Y')}\nПасека: {row['apiary']}\nУлей: {row['hive_code']}\n\n"
                f"{row['anomaly_description'] or 'Отклонение'}\n\nВес: {row['hive_weight_kg']} кг\n"
                f"Т: {row['internal_temp_c']} °C\nВлажн.: {row['internal_humidity_pct']}%\nCO₂: {row['co2_ppm']}"
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
        self.load_filters()
        self.chart()
        self.alerts()
        db_counts = self.db.execute('SELECT COUNT(*) FROM hives').fetchone()[0]
        log_counts = self.db.execute('SELECT COUNT(*) FROM hive_sensor_logs').fetchone()[0]
        self.db_text.setText(f'SQLite · {db_counts} ульев · {log_counts} записей')
        # Очищаем старый контекст диалога при загрузке новой базы
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
        btn_apply = buttons.addButton('Записать в БД', QDialogButtonBox.AcceptRole)
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
        "#chatScroll { background: #FFF9F7; border: 0; }"
        "#chatContainer { background: #FFF9F7; }"
        "#chatInputBar { background: white; border-top: 1px solid " + BORDER + "; }"
        "#chatInput { background: #FAFAFC; border: 1px solid " + BORDER + "; border-radius: 12px; padding: 10px 16px; font-size: 13px; }"
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
