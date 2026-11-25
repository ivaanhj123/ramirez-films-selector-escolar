@echo off
cd /d "%~dp0"
echo Instalando componentes necesarios...
python -m pip install -r requirements_selector.txt
if errorlevel 1 (
  echo No se pudieron instalar los componentes. Revisa tu conexion a Internet.
  pause
  exit /b 1
)
python selector_fotos_escolares.py
if errorlevel 1 pause
