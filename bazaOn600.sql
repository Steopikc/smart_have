-- ============================================================
-- ЕДИНАЯ (ДЕНОРМАЛИЗОВАННАЯ) ТАБЛИЦА ДЛЯ УДОБСТВА ЧТЕНИЯ
-- СУБД: SQLite
-- Период: 2024-01-01 — 2026-12-31
-- Ульев: 600
-- ============================================================

DROP TABLE IF EXISTS smart_hive_full_log;

CREATE TABLE smart_hive_full_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_date TEXT NOT NULL,
    
    -- Идентификация улья и кучки
    site_id INTEGER NOT NULL,
    site_name TEXT NOT NULL,
    hive_id INTEGER NOT NULL,
    hive_label TEXT NOT NULL,
    
    -- Погода на улице (привязана к кучке)
    outdoor_temp_avg REAL,
    outdoor_temp_min REAL,
    outdoor_temp_max REAL,
    outdoor_humidity REAL,
    precipitation_mm REAL,
    wind_speed_ms REAL,
    
    -- Показания датчиков внутри улья
    hive_temp REAL,              -- Температура внутри
    hive_humidity REAL,          -- Влажность внутри
    co2_level REAL,              -- CO2
    weight_kg REAL,              -- Вес улья
    sound_db REAL,               -- Звук
    bee_activity INTEGER,        -- Активность пчел
    is_anomaly INTEGER DEFAULT 0,   -- Флаг аномалии (1 - да, 0 - нет)
    anomaly_note TEXT,           -- Описание аномалии
    
    -- События за день
    lid_opened_by TEXT,          -- Кто открывал крышку ('beekeeper', 'bear', NULL)
    was_tipped_over INTEGER DEFAULT 0, -- Был ли опрокинут (1 - да, 0 - нет)
    tipped_by TEXT               -- Кто опрокинул ('bear', NULL)
);

-- ============================================================
-- ГЕНЕРАЦИЯ ДАННЫХ
-- ============================================================

-- Вставляем данные с помощью рекурсивного CTE.
-- Генерируем 1096 дней для каждого из 600 ульев.

INSERT INTO smart_hive_full_log (
    log_date, site_id, site_name, hive_id, hive_label,
    outdoor_temp_avg, outdoor_temp_min, outdoor_temp_max, outdoor_humidity, precipitation_mm, wind_speed_ms,
    hive_temp, hive_humidity, co2_level, weight_kg, sound_db, bee_activity,
    is_anomaly, anomaly_note, lid_opened_by, was_tipped_over, tipped_by
)
WITH RECURSIVE
  -- 1. Генерируем порядковые номера дней (от 0 до 1095)
  days(n) AS (
    SELECT 0 UNION ALL SELECT n + 1 FROM days WHERE n < 1095
  ),
  -- 2. Справочник кучек (site_id и их названия)
  sites(site_id, site_name) AS (
    SELECT 1, 'Кучка А — Бурзянский бор' UNION ALL 
    SELECT 2, 'Кучка Б — Архангельский лес' UNION ALL 
    SELECT 3, 'Кучка В — Гафурийский' UNION ALL 
    SELECT 4, 'Кучка Г — Нуримановский' UNION ALL 
    SELECT 5, 'Кучка Д — Иглинский' UNION ALL 
    SELECT 6, 'Кучка Е — Шаранский' UNION ALL 
    SELECT 7, 'Кучка Ж — Белорецкий' UNION ALL 
    SELECT 8, 'Кучка З — Мишкинский'
  ),
  -- 3. Справочник ульев (600 штук, по 75 на кучку)
  hives_list AS (
    SELECT 
      s.site_id, 
      s.site_name,
      1 + (hive_id - 1) / 75 AS calc_site_id, -- определяем к какой кучке относится улей
      hive_id,
      hive_label
    FROM (SELECT 1 as id) dummy
    CROSS JOIN (SELECT site_id, site_name FROM sites) s
    CROSS JOIN (
       SELECT 1 AS hive_id UNION ALL SELECT 2 UNION ALL SELECT 3 -- (для краткости кода, здесь должен быть полный список до 75)
       -- В реальности мы генерируем ульи через отдельный CTE, но для одного запроса 
       -- сделаем это через хитрый JOIN ниже
    ) x
  ),
  -- Генерируем 600 ульев
  hives_600 AS (
    SELECT 
      s.site_id,
      s.site_name,
      s.site_id * 100 + n.n AS hive_id, -- Уникальный ID улья (например, 101, 102... 875)
      s.site_id || '-' || printf('%02d', n.n) AS hive_label
    FROM sites s
    CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10
                UNION ALL SELECT 11 UNION ALL SELECT 12 UNION ALL SELECT 13 UNION ALL SELECT 14 UNION ALL SELECT 15 UNION ALL SELECT 16 UNION ALL SELECT 17 UNION ALL SELECT 18 UNION ALL SELECT 19 UNION ALL SELECT 20
                UNION ALL SELECT 21 UNION ALL SELECT 22 UNION ALL SELECT 23 UNION ALL SELECT 24 UNION ALL SELECT 25 UNION ALL SELECT 26 UNION ALL SELECT 27 UNION ALL SELECT 28 UNION ALL SELECT 29 UNION ALL SELECT 30
                UNION ALL SELECT 31 UNION ALL SELECT 32 UNION ALL SELECT 33 UNION ALL SELECT 34 UNION ALL SELECT 35 UNION ALL SELECT 36 UNION ALL SELECT 37 UNION ALL SELECT 38 UNION ALL SELECT 39 UNION ALL SELECT 40
                UNION ALL SELECT 41 UNION ALL SELECT 42 UNION ALL SELECT 43 UNION ALL SELECT 44 UNION ALL SELECT 45 UNION ALL SELECT 46 UNION ALL SELECT 47 UNION ALL SELECT 48 UNION ALL SELECT 49 UNION ALL SELECT 50
                UNION ALL SELECT 51 UNION ALL SELECT 52 UNION ALL SELECT 53 UNION ALL SELECT 54 UNION ALL SELECT 55 UNION ALL SELECT 56 UNION ALL SELECT 57 UNION ALL SELECT 58 UNION ALL SELECT 59 UNION ALL SELECT 60
                UNION ALL SELECT 61 UNION ALL SELECT 62 UNION ALL SELECT 63 UNION ALL SELECT 64 UNION ALL SELECT 65 UNION ALL SELECT 66 UNION ALL SELECT 67 UNION ALL SELECT 68 UNION ALL SELECT 69 UNION ALL SELECT 70
                UNION ALL SELECT 71 UNION ALL SELECT 72 UNION ALL SELECT 73 UNION ALL SELECT 74 UNION ALL SELECT 75) n
  )
SELECT
    -- ДАТА
    date('2024-01-01', '+' || d.n || ' days') AS log_date,
    
    -- ИДЕНТИФИКАЦИЯ
    h.site_id,
    h.site_name,
    h.hive_id,
    h.hive_label,
    
    -- ПОГОДА (расчет по сезону с шумом)
    ROUND(-15 + 35 * sin(3.14159 * (CAST(strftime('%j', date('2024-01-01', '+' || d.n || ' days')) AS REAL) - 100) / 365.0) + ((ABS(RANDOM() % 100) / 100.0) - 0.5) * 10 - CASE WHEN h.site_id = 7 THEN 3 ELSE 0 END, 1),
    ROUND(-15 + 35 * sin(3.14159 * (CAST(strftime('%j', date('2024-01-01', '+' || d.n || ' days')) AS REAL) - 100) / 365.0) + ((ABS(RANDOM() % 100) / 100.0) - 0.5) * 10 - CASE WHEN h.site_id = 7 THEN 3 ELSE 0 END - 3 - (ABS(RANDOM() % 400) / 100.0), 1),
    ROUND(-15 + 35 * sin(3.14159 * (CAST(strftime('%j', date('2024-01-01', '+' || d.n || ' days')) AS REAL) - 100) / 365.0) + ((ABS(RANDOM() % 100) / 100.0) - 0.5) * 10 - CASE WHEN h.site_id = 7 THEN 3 ELSE 0 END + 3 + (ABS(RANDOM() % 400) / 100.0), 1),
    ROUND(60 + (ABS(RANDOM() % 250) / 10.0), 1),
    ROUND(CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.35 THEN (ABS(RANDOM() % 150) / 10.0) ELSE 0 END, 1),
    ROUND(1 + (ABS(RANDOM() % 50) / 10.0), 1),
    
    -- ДАТЧИКИ ВНУТРИ (зависит от сезона)
    ROUND(
        CASE 
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (12,1,2) THEN 24 + (ABS(RANDOM() % 40) / 10.0)
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (6,7,8) THEN 33 + (ABS(RANDOM() % 25) / 10.0)
            ELSE 30 + (ABS(RANDOM() % 50) / 10.0)
        END + CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.02 THEN 8.0 WHEN (ABS(RANDOM() % 100) / 100.0) > 0.98 THEN -8.0 ELSE 0 END, 2),
    ROUND(
        CASE 
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (12,1,2) THEN 55 + (ABS(RANDOM() % 150) / 10.0)
            ELSE 60 + (ABS(RANDOM() % 180) / 10.0)
        END + CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.02 THEN 25.0 ELSE 0 END, 1),
    ROUND(
        CASE 
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (12,1,2) THEN 2.5 + (ABS(RANDOM() % 20) / 10.0)
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (6,7,8) THEN 0.3 + (ABS(RANDOM() % 5) / 10.0)
            ELSE 0.8 + (ABS(RANDOM() % 12) / 10.0)
        END + CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.02 THEN 3.0 ELSE 0 END, 2),
    ROUND(
        CASE 
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (12,1,2) THEN 35 + (ABS(RANDOM() % 100) / 10.0)
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (6,7,8) THEN 40 + (CAST(strftime('%j', date('2024-01-01', '+' || d.n || ' days')) AS REAL) / 365.0) * 25 + (ABS(RANDOM() % 80) / 10.0)
            ELSE 38 + (ABS(RANDOM() % 150) / 10.0)
        END - CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.02 THEN 10.0 ELSE 0 END, 1),
    ROUND(
        CASE 
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (12,1,2) THEN 28 + (ABS(RANDOM() % 100) / 10.0)
            WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (6,7,8) THEN 45 + (ABS(RANDOM() % 200) / 10.0)
            ELSE 35 + (ABS(RANDOM() % 150) / 10.0)
        END, 1),
    CASE 
        WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (12,1,2) THEN 0
        WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) IN (6,7,8) THEN 80 + (ABS(RANDOM() % 120))
        ELSE 20 + (ABS(RANDOM() % 60))
    END,
    
    -- АНОМАЛИИ
    CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.04 THEN 1 ELSE 0 END,
    CASE WHEN (ABS(RANDOM() % 100) / 100.0) < 0.04 THEN 'Обнаружено отклонение в показаниях датчиков' ELSE NULL END,
    
    -- СОБЫТИЯ ЗА ДЕНЬ (Открытие крышки пчеловодом, только весна-лето, шанс 10% в день)
    CASE 
        WHEN CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) BETWEEN 4 AND 8 
             AND (ABS(RANDOM() % 100) / 100.0) < 0.1 
        THEN 'beekeeper' 
        ELSE NULL 
    END,
    
    -- СОБЫТИЯ ЗА ДЕНЬ (Опрокидывание медведем, только кучки 6 и 7, шанс 0.5% в день)
    CASE 
        WHEN h.site_id IN (6, 7) 
             AND (ABS(RANDOM() % 1000) / 1000.0) < 0.005 
        THEN 1 
        ELSE 0 
    END,
    CASE 
        WHEN h.site_id IN (6, 7) 
             AND (ABS(RANDOM() % 1000) / 1000.0) < 0.005 
        THEN 'bear' 
        ELSE NULL 
    END

FROM hives_600 h
CROSS JOIN days d;
