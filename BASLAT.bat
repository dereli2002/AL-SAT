@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto ortam_hazir
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv .venv
) else (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m venv .venv
  ) else (
    echo Python bulunamadi. Python 3.12 yukleyin ve tekrar BASLAT.bat dosyasini acin.
    pause
    exit /b 1
  )
)
if errorlevel 1 goto hata
:ortam_hazir
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto hata
".venv\Scripts\python.exe" baslat.py
if errorlevel 1 goto hata
exit /b 0
:hata
echo Baslatma tamamlanamadi. Yukaridaki hata mesajini kontrol edin.
pause
exit /b 1
