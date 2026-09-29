@echo off
chcp 65001 >nul
rem Сборка Dictate.exe. Перед этим: python -m venv .venv и .venv\Scripts\pip install -r requirements.txt pyinstaller
cd /d "%~dp0"
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --noconsole ^
  --name Dictate --icon dictate.ico ^
  --collect-data faster_whisper ^
  --collect-binaries ctranslate2 ^
  --exclude-module nvidia --exclude-module psutil --exclude-module matplotlib ^
  app.py
echo.
echo Готово: dist\Dictate.exe
