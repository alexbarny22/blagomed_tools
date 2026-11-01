@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -q --disable-pip-version-check pillow requests
echo 1 - проверить нейросети на фото из папки test_photos
echo 2 - показать список моделей агрегатора
set /p ch=Введите 1 или 2 и нажмите Enter: 
if "%ch%"=="2" (python test_models.py list) else (python test_models.py)
pause
