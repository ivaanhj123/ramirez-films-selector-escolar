@echo off
cd /d "%~dp0"
python selector_fotos_escolares.py
if errorlevel 1 pause
