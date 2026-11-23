"""
data_io.py — импорт базы данных и загрузка телеметрии по проводу (COM/USB).

Поддерживаемые форматы по проводу (строка за строкой):
  1) JSON: {"hive_code":"RB-HIVE-0001","record_datetime":"2026-06-01 14:00:00",
            "internal_temp_c":34.2,"internal_humidity_pct":55,...}
  2) CSV с заголовком (разделитель ; или ,)
  3) Простой key=value;key=value (для отладочных прошивок)

Обязательные поля для записи в hive_sensor_logs:
  hive_id ИЛИ hive_code, и record_date ИЛИ record_datetime
Остальные поля — по возможности. Схема (record_date vs record_datetime,
daily_ vs hourly_weight_delta_kg) определяется автоматически.
Поддерживаются pesticide_risk и hive_health_index (basafor25bees.sql).
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Импорт / подключение готовой БД
# ---------------------------------------------------------------------------

REQUIRED_TABLES = ("apiaries", "hives", "hive_sensor_logs")


def connect(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def validate_db(path: str | Path) -> tuple[bool, str]:
    """Проверяет SQLite-файл и минимально необходимую структуру проекта."""
    p = Path(path)
    if not p.exists():
        return False, f"Файл не найден: {p}"
    try:
        c = connect(p)
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in tables]
            if missing:
                return False, f"В базе нет таблиц: {', '.join(missing)}"

            required = {
                "apiaries": {"apiary_id", "name"},
                "hives": {"hive_id", "apiary_id", "hive_code"},
            }
            for table, needed in required.items():
                actual = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                absent = sorted(needed - actual)
                if absent:
                    return False, f"В таблице {table} отсутствуют поля: {', '.join(absent)}"

            actual = {r[1] for r in c.execute("PRAGMA table_info(hive_sensor_logs)")}
            if "hive_id" not in actual:
                return False, "В hive_sensor_logs отсутствует поле hive_id"
            if "record_date" not in actual and "record_datetime" not in actual:
                return False, "В hive_sensor_logs нет ни record_date, ни record_datetime"

            check = c.execute("PRAGMA quick_check").fetchone()
            if check and check[0] != "ok":
                return False, f"Проверка целостности SQLite: {check[0]}"
        finally:
            c.close()
    except sqlite3.Error as e:
        return False, f"Не удалось открыть SQLite: {e}"
    return True, "OK"


def import_database(
    source: str | Path,
    target: str | Path,
    *,
    mode: str = "replace",
) -> str:
    """
    mode:
      replace — полностью заменить target копией source
      merge   — добавить недостающие записи (простая вставка с IGNORE)
    """
    source = Path(source)
    target = Path(target)
    ok, msg = validate_db(source)
    if not ok:
        raise RuntimeError(msg)

    if mode == "replace":
        if source.resolve() == target.resolve():
            return f"Эта база уже активна: {target.name}"
        temp = target.with_name(target.stem + ".replacement" + target.suffix)
        try:
            shutil.copy2(source, temp)
            ok, msg = validate_db(temp)
            if not ok:
                raise RuntimeError(msg)
            if target.exists():
                target.unlink()
            temp.replace(target)
        finally:
            if temp.exists():
                temp.unlink()
        return f"База заменена: {target.name}"

    if mode == "merge":
        ok_target, target_msg = validate_db(target)
        if not ok_target:
            raise RuntimeError(f"Текущая база не подходит для слияния: {target_msg}")
        src = connect(source)
        dst = connect(target)
        try:
            src_tables = {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            dst_tables = {r[0] for r in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            weather_tables = [t for t in ("daily_weather", "hourly_weather")
                              if t in src_tables and t in dst_tables]

            merge_tables = ["apiaries", "hives", *weather_tables, "hive_sensor_logs"]

            for table in merge_tables:
                src_cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
                dst_cols = {r[1] for r in dst.execute(f"PRAGMA table_info({table})")}
                missing_cols = src_cols - dst_cols
                if missing_cols:
                    raise RuntimeError(
                        f"Слияние невозможно: в целевой таблице {table} нет полей "
                        f"{', '.join(sorted(missing_cols))}. Используйте замену базы."
                    )

            for table, key, compare_cols in (
                ("apiaries", "apiary_id", ("name",)),
                ("hives", "hive_id", ("apiary_id", "hive_code")),
            ):
                src_names = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
                dst_names = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
                for row in src.execute(f"SELECT * FROM {table}"):
                    existing = dst.execute(
                        f"SELECT * FROM {table} WHERE {key}=?",
                        (row[src_names.index(key)],)
                    ).fetchone()
                    if existing:
                        for col in compare_cols:
                            if col in src_names and col in dst_names:
                                if existing[dst_names.index(col)] != row[src_names.index(col)]:
                                    raise RuntimeError(
                                        f"Конфликт {table}.{key}: ID "
                                        f"{row[src_names.index(key)]} уже занят "
                                        f"другой записью. Используйте замену базы."
                                    )

            # справочники + погода: INSERT OR IGNORE,
            # у hourly_weather удаляем искусственный weather_id.
            for table in ["apiaries", "hives", *weather_tables]:
                cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
                if not cols:
                    continue
                drop_col = ("weather_id" if table in ("daily_weather", "hourly_weather")
                            and "weather_id" in cols else None)
                cols_use = [c for c in cols if c != drop_col] if drop_col else cols
                col_list = ", ".join(cols_use)
                placeholders = ", ".join("?" * len(cols_use))
                data = []
                for r in src.execute(f"SELECT {', '.join(cols)} FROM {table}"):
                    t = list(r)
                    if drop_col:
                        t.pop(cols.index(drop_col))
                    data.append(tuple(t))
                dst.executemany(
                    f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
                    data,
                )

            # телеметрия: всегда убираем log_id, чтобы PK не конфликтовал
            cols = [r[1] for r in src.execute("PRAGMA table_info(hive_sensor_logs)").fetchall()]
            if "log_id" in cols:
                drop_idx = cols.index("log_id")
                cols_use = [c for c in cols if c != "log_id"]
            else:
                drop_idx = -1
                cols_use = cols
            col_list = ", ".join(cols_use)
            placeholders = ", ".join("?" * len(cols_use))
            before = dst.execute("SELECT COUNT(*) FROM hive_sensor_logs").fetchone()[0]
            data = []
            for r in src.execute(f"SELECT {', '.join(cols)} FROM hive_sensor_logs"):
                t = list(r)
                if drop_idx >= 0:
                    t.pop(drop_idx)
                data.append(tuple(t))
            dst.executemany(
                f"INSERT OR IGNORE INTO hive_sensor_logs ({col_list}) VALUES ({placeholders})",
                data,
            )
            after = dst.execute("SELECT COUNT(*) FROM hive_sensor_logs").fetchone()[0]
            dst.commit()
            return f"Слияние завершено. Записей телеметрии: было {before}, стало {after}."
        finally:
            src.close()
            dst.close()

    raise ValueError(f"Неизвестный mode: {mode}")


def import_sql_script(sql_path: str | Path, target: str | Path) -> str:
    """Создаёт БД из SQL во временный файл и заменяет рабочую только после проверки."""
    sql_path, target = Path(sql_path), Path(target)
    if not sql_path.exists():
        raise FileNotFoundError(sql_path)
    temp = target.with_name(target.stem + ".importing" + target.suffix)
    if temp.exists():
        temp.unlink()
    try:
        c = sqlite3.connect(str(temp))
        try:
            c.executescript(sql_path.read_text(encoding="utf-8-sig"))
            c.commit()
        finally:
            c.close()
        ok, msg = validate_db(temp)
        if not ok:
            raise RuntimeError(f"SQL-файл создал неподходящую базу: {msg}")
        if target.exists():
            target.unlink()
        temp.replace(target)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise
    return f"База создана из {sql_path.name}"


# ---------------------------------------------------------------------------
# Парсинг строк с датчика
# ---------------------------------------------------------------------------

# [25 BEES] добавлены pesticide_risk и hive_health_index
SENSOR_FIELDS = [
    "hive_id", "hive_code", "record_date", "record_datetime",
    "internal_temp_c", "internal_humidity_pct", "co2_ppm",
    "hive_weight_kg", "daily_weight_delta_kg", "hourly_weight_delta_kg",
    "lid_opened_count", "lid_event_cause", "is_tipped_over", "tipping_cause",
    "sound_level_db", "bee_activity_index", "estimated_flights_count",
    "pesticide_risk", "hive_health_index",
    "ambient_temp_c", "anomaly_flag", "anomaly_description",
]

NUMERIC_FIELDS = {
    "hive_id", "internal_temp_c", "internal_humidity_pct", "co2_ppm",
    "hive_weight_kg", "daily_weight_delta_kg", "hourly_weight_delta_kg",
    "lid_opened_count", "is_tipped_over", "sound_level_db",
    "bee_activity_index", "estimated_flights_count",
    "pesticide_risk", "hive_health_index",
    "ambient_temp_c", "anomaly_flag",
}


def _coerce(field: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if field in NUMERIC_FIELDS:
        try:
            if field in ("hive_id", "co2_ppm", "lid_opened_count", "is_tipped_over",
                         "bee_activity_index", "estimated_flights_count",
                         "pesticide_risk", "anomaly_flag"):
                return int(float(str(value).replace(",", ".")))
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return None
    return str(value)


def parse_sensor_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    data: dict[str, Any] = {}

    if line.startswith("{"):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in SENSOR_FIELDS:
                        data[k] = _coerce(k, v)
                return data or None
        except json.JSONDecodeError:
            pass

    if "=" in line and ("|" in line or ";" in line or "," in line):
        parts = re.split(r"[|;]", line)
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in SENSOR_FIELDS:
                data[k] = _coerce(k, v)
        if data:
            return data

    return None


def parse_csv_block(text: str) -> list[dict[str, Any]]:
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if text.count(";") >= text.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows: list[dict[str, Any]] = []
    for raw in reader:
        item: dict[str, Any] = {}
        for k, v in raw.items():
            if k is None:
                continue
            key = k.strip()
            if key in SENSOR_FIELDS:
                item[key] = _coerce(key, v)
        if item:
            rows.append(item)
    return rows


def resolve_hive_id(db: sqlite3.Connection, row: dict[str, Any]) -> int | None:
    if row.get("hive_id") is not None:
        return int(row["hive_id"])
    code = row.get("hive_code")
    if code:
        r = db.execute(
            "SELECT hive_id FROM hives WHERE hive_code = ?", (str(code),)
        ).fetchone()
        if r:
            return int(r[0])
    return None


def insert_sensor_rows(
    db: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    default_date: str | None = None,
) -> tuple[int, int, list[str]]:
    """
    Вставляет строки в hive_sensor_logs, автоматически определяя
    схему (record_date vs record_datetime, daily_ vs hourly_weight_delta_kg).
    [25 BEES] Учитывает pesticide_risk, hive_health_index.
    Returns: (inserted, skipped, errors)
    """
    inserted = 0
    skipped = 0
    errors: list[str] = []

    table_cols = {r[1] for r in db.execute("PRAGMA table_info(hive_sensor_logs)")}
    if "hive_id" not in table_cols:
        return 0, len(rows), ["В hive_sensor_logs отсутствует hive_id"]

    time_col = "record_datetime" if "record_datetime" in table_cols else "record_date"

    # [25 BEES] добавили pesticide_risk и hive_health_index
    candidate = [
        "internal_temp_c", "internal_humidity_pct", "co2_ppm",
        "hive_weight_kg", "daily_weight_delta_kg", "hourly_weight_delta_kg",
        "lid_opened_count", "lid_event_cause", "is_tipped_over", "tipping_cause",
        "sound_level_db", "bee_activity_index", "estimated_flights_count",
        "pesticide_risk", "hive_health_index",
        "ambient_temp_c", "anomaly_flag", "anomaly_description",
    ]
    payload_cols = [c for c in candidate if c in table_cols]

    all_cols = ["hive_id", time_col] + payload_cols
    sql = (
        f"INSERT INTO hive_sensor_logs ({', '.join(all_cols)}) "
        f"VALUES ({', '.join('?' * len(all_cols))})"
    )

    defaults = {
        "internal_temp_c": 0.0, "internal_humidity_pct": 0.0,
        "co2_ppm": 0, "hive_weight_kg": 0.0,
        "daily_weight_delta_kg": 0.0, "hourly_weight_delta_kg": 0.0,
        "lid_opened_count": 0, "lid_event_cause": "Нет",
        "is_tipped_over": 0, "tipping_cause": "Нет",
        "sound_level_db": 0.0, "bee_activity_index": 0,
        "estimated_flights_count": 0,
        "pesticide_risk": 0, "hive_health_index": 5.0,
        "ambient_temp_c": 0.0, "anomaly_flag": 0, "anomaly_description": None,
    }

    for i, row in enumerate(rows, 1):
        try:
            hive_id = resolve_hive_id(db, row)
            if hive_id is None:
                skipped += 1
                errors.append(f"Строка {i}: не найден улей (hive_id/hive_code)")
                continue
            rec_time = (
                row.get(time_col)
                or row.get("record_datetime")
                or row.get("record_date")
                or default_date
                or date.today().isoformat()
            )
            values: list[Any] = [hive_id, rec_time]
            for c in payload_cols:
                v = row.get(c)
                if v is None:
                    v = defaults.get(c)
                values.append(v)
            db.execute(sql, values)
            inserted += 1
        except Exception as e:  # noqa: BLE001
            skipped += 1
            errors.append(f"Строка {i}: {e}")

    db.commit()
    return inserted, skipped, errors


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports()]


def read_serial_lines(
    port: str,
    baud: int = 115200,
    *,
    timeout: float = 1.0,
    max_lines: int = 500,
    duration_sec: float = 8.0,
    on_line: Callable[[str], None] | None = None,
) -> list[str]:
    """Читает строки с COM-порта ограниченное время."""
    try:
        import serial
        import time
    except ImportError as e:
        raise RuntimeError(
            "Не установлен пакет pyserial. Выполните: pip install pyserial"
        ) from e

    lines: list[str] = []
    ser = serial.Serial(port, baudrate=baud, timeout=timeout)
    try:
        t0 = time.time()
        buf = ""
        while time.time() - t0 < duration_sec and len(lines) < max_lines:
            chunk = ser.read(256)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    lines.append(line)
                    if on_line:
                        on_line(line)
        if buf.strip():
            lines.append(buf.strip())
    finally:
        ser.close()
    return lines
