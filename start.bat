@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверьте Python и интернет-соединение.
  pause
  exit /b 1
)
py app.py
pause
