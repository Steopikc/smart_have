INSERT INTO smart_hive_full_log (
    log_date, site_id, site_name, hive_id, hive_label,
    outdoor_temp_avg, outdoor_temp_min, outdoor_temp_max, outdoor_humidity, precipitation_mm, wind_speed_ms,
    hive_temp, hive_humidity, co2_level, weight_kg, sound_db, bee_activity,
    is_anomaly, anomaly_note, lid_opened_by, was_tipped_over, tipped_by
)
WITH RECURSIVE
  days(n) AS (
    SELECT 0 UNION ALL SELECT n + 1 FROM days WHERE n < 1095
  ),
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
  hives_600 AS (
    SELECT
      s.site_id,
      s.site_name,
      s.site_id * 100 + n.n AS hive_id,
      s.site_id || '-' || printf('%02d', n.n) AS hive_label
    FROM sites s
    CROSS JOIN (
      SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5
      UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10
      UNION ALL SELECT 11 UNION ALL SELECT 12 UNION ALL SELECT 13 UNION ALL SELECT 14 UNION ALL SELECT 15
      UNION ALL SELECT 16 UNION ALL SELECT 17 UNION ALL SELECT 18 UNION ALL SELECT 19 UNION ALL SELECT 20
      UNION ALL SELECT 21 UNION ALL SELECT 22 UNION ALL SELECT 23 UNION ALL SELECT 24 UNION ALL SELECT 25
      UNION ALL SELECT 26 UNION ALL SELECT 27 UNION ALL SELECT 28 UNION ALL SELECT 29 UNION ALL SELECT 30
      UNION ALL SELECT 31 UNION ALL SELECT 32 UNION ALL SELECT 33 UNION ALL SELECT 34 UNION ALL SELECT 35
      UNION ALL SELECT 36 UNION ALL SELECT 37 UNION ALL SELECT 38 UNION ALL SELECT 39 UNION ALL SELECT 40
      UNION ALL SELECT 41 UNION ALL SELECT 42 UNION ALL SELECT 43 UNION ALL SELECT 44 UNION ALL SELECT 45
      UNION ALL SELECT 46 UNION ALL SELECT 47 UNION ALL SELECT 48 UNION ALL SELECT 49 UNION ALL SELECT 50
      UNION ALL SELECT 51 UNION ALL SELECT 52 UNION ALL SELECT 53 UNION ALL SELECT 54 UNION ALL SELECT 55
      UNION ALL SELECT 56 UNION ALL SELECT 57 UNION ALL SELECT 58 UNION ALL SELECT 59 UNION ALL SELECT 60
      UNION ALL SELECT 61 UNION ALL SELECT 62 UNION ALL SELECT 63 UNION ALL SELECT 64 UNION ALL SELECT 65
      UNION ALL SELECT 66 UNION ALL SELECT 67 UNION ALL SELECT 68 UNION ALL SELECT 69 UNION ALL SELECT 70
      UNION ALL SELECT 71 UNION ALL SELECT 72 UNION ALL SELECT 73 UNION ALL SELECT 74 UNION ALL SELECT 75
    ) n
  ),
  -- Общие вычисляемые поля на день/улей (чтобы не дублировать формулы)
  base AS (
    SELECT
      h.site_id, h.site_name, h.hive_id, h.hive_label,
      d.n,
      date('2024-01-01', '+' || d.n || ' days') AS log_date,
      CAST(strftime('%j', date('2024-01-01', '+' || d.n || ' days')) AS REAL) AS doy,
      CAST(strftime('%m', date('2024-01-01', '+' || d.n || ' days')) AS INTEGER) AS mon,
      -- детерминированная "волна" по улью и дню
      sin(h.hive_id * 0.7 + d.n * 0.13) AS w1,
      sin(h.hive_id * 0.31 + d.n * 0.07) AS w2,
      cos(h.hive_id * 0.19 + d.n * 0.23) AS w3
    FROM hives_600 h
    CROSS JOIN days d
  )
SELECT
    b.log_date,
    b.site_id,
    b.site_name,
    b.hive_id,
    b.hive_label,

    -- ПОГОДА: сезонная кривая (реальный 2024 профиль) + поправка на кучку + детерм. вариация
    ROUND(
      ( -8.0 + 22.0 * sin( 3.14159 * (b.doy - 100) / 365.0 ) )   -- сезон: зима ≈ -8..-19, лето ≈ +14..+26
      + 3.0 * b.w1
      - CASE WHEN b.site_id = 7 THEN 3 ELSE 0 END                 -- Белорецкий холоднее
      , 1) AS outdoor_temp_avg,
    ROUND(
      ( -8.0 + 22.0 * sin( 3.14159 * (b.doy - 100) / 365.0 ) )
      + 3.0 * b.w1
      - CASE WHEN b.site_id = 7 THEN 3 ELSE 0 END
      - 3.0 - 1.5 * abs(b.w2)
      , 1) AS outdoor_temp_min,
    ROUND(
      ( -8.0 + 22.0 * sin( 3.14159 * (b.doy - 100) / 365.0 ) )
      + 3.0 * b.w1
      - CASE WHEN b.site_id = 7 THEN 3 ELSE 0 END
      + 3.0 + 1.5 * abs(b.w2)
      , 1) AS outdoor_temp_max,

    -- Влажность наружная: выше зимой/осенью, ниже летом
    ROUND( 70.0 - 15.0 * sin( 3.14159 * (b.doy - 100) / 365.0 ) + 5.0 * b.w3, 1) AS outdoor_humidity,

    -- Осадки: детерминированное "расписание" — чаще июнь–август, суше сентябрь
    ROUND(
      CASE
        WHEN b.mon IN (6,7,8) AND (b.n + b.hive_id) % 3 = 0 THEN 2.0 + 6.0 * abs(b.w1)
        WHEN b.mon = 9 AND (b.n + b.hive_id) % 11 = 0 THEN 0.5 + 2.0 * abs(b.w2)
        WHEN b.mon IN (4,5,10,11) AND (b.n + b.hive_id) % 5 = 0 THEN 1.0 + 3.0 * abs(b.w3)
        ELSE 0.0
      END, 1) AS precipitation_mm,

    -- Ветер: весна/осень сильнее, лето слабее
    ROUND( 3.2 + 1.0 * sin( 3.14159 * (b.doy - 60) / 365.0 ) + 0.6 * abs(b.w1), 1) AS wind_speed_ms,

    -- ВНУТРЕННИЕ ДАТЧИКИ (сезон + поправка на улей + детерм. вариация)
    ROUND(
      CASE
        WHEN b.mon IN (12,1,2) THEN 24.0 + 2.0 * abs(b.w1)
        WHEN b.mon IN (6,7,8)  THEN 33.0 + 1.5 * abs(b.w2)
        ELSE 30.0 + 2.5 * abs(b.w3)
      END
      + CASE WHEN b.hive_id % 37 = 0 AND b.n % 50 = 0 THEN 6.0 ELSE 0.0 END  -- детерм. "аномальный" улей
      , 2) AS hive_temp,

    ROUND(
      CASE
        WHEN b.mon IN (12,1,2) THEN 55.0 + 3.0 * abs(b.w1)
        ELSE 60.0 + 4.0 * abs(b.w2)
      END
      + CASE WHEN b.hive_id % 41 = 0 AND b.n % 60 = 0 THEN 15.0 ELSE 0.0 END
      , 1) AS hive_humidity,

    ROUND(
      CASE
        WHEN b.mon IN (12,1,2) THEN 2.5 + 0.6 * abs(b.w3)
        WHEN b.mon IN (6,7,8)  THEN 0.3 + 0.2 * abs(b.w1)
        ELSE 0.8 + 0.5 * abs(b.w2)
      END
      + CASE WHEN b.hive_id % 43 = 0 AND b.n % 70 = 0 THEN 2.0 ELSE 0.0 END
      , 2) AS co2_level,

    ROUND(
      CASE
        WHEN b.mon IN (12,1,2) THEN 35.0 + 2.0 * abs(b.w1)
        WHEN b.mon IN (6,7,8)  THEN 40.0 + 5.0 * abs(b.w2)
        ELSE 38.0 + 4.0 * abs(b.w3)
      END
      - CASE WHEN b.hive_id % 47 = 0 AND b.n % 80 = 0 THEN 8.0 ELSE 0.0 END
      , 1) AS weight_kg,

    ROUND(
      CASE
        WHEN b.mon IN (12,1,2) THEN 28.0 + 2.0 * abs(b.w1)
        WHEN b.mon IN (6,7,8)  THEN 45.0 + 4.0 * abs(b.w2)
        ELSE 35.0 + 3.0 * abs(b.w3)
      END
      , 1) AS sound_db,

    -- Активность: зимой 0, летом высокая, межсезонье средняя
    CASE
      WHEN b.mon IN (12,1,2) THEN 0
      WHEN b.mon IN (6,7,8)  THEN 80 + CAST(40.0 * abs(b.w1) AS INTEGER)
      ELSE 20 + CAST(30.0 * abs(b.w2) AS INTEGER)
    END AS bee_activity,

    -- АНОМАЛИИ: детерминированное окно, а не случайный шанс
    CASE
      WHEN (b.hive_id % 37 = 0 AND b.n % 50 = 0)
        OR (b.hive_id % 41 = 0 AND b.n % 60 = 0)
        OR (b.hive_id % 43 = 0 AND b.n % 70 = 0)
        OR (b.hive_id % 47 = 0 AND b.n % 80 = 0)
      THEN 1 ELSE 0
    END AS is_anomaly,
    CASE
      WHEN (b.hive_id % 37 = 0 AND b.n % 50 = 0)
        OR (b.hive_id % 41 = 0 AND b.n % 60 = 0)
        OR (b.hive_id % 43 = 0 AND b.n % 70 = 0)
        OR (b.hive_id % 47 = 0 AND b.n % 80 = 0)
      THEN 'Обнаружено отклонение в показаниях датчиков'
      ELSE NULL
    END AS anomaly_note,

    -- ПЧЕЛОВОД открывает крышку: только апрель–август, детерм. шаг по улью
    CASE
      WHEN b.mon BETWEEN 4 AND 8 AND (b.n + b.hive_id) % 10 = 0
      THEN 'beekeeper' ELSE NULL
    END AS lid_opened_by,

    -- МЕДВЕДЬ: только кучки 6 и 7, детерм. редкое событие
    CASE
      WHEN b.site_id IN (6,7) AND (b.n * 7 + b.hive_id) % 997 = 0
      THEN 1 ELSE 0
    END AS was_tipped_over,
    CASE
      WHEN b.site_id IN (6,7) AND (b.n * 7 + b.hive_id) % 997 = 0
      THEN 'bear' ELSE NULL
    END AS tipped_by

FROM base b;
