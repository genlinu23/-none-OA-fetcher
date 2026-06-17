@echo off
setlocal
pushd "%~dp0.."
set "PYTHON_EXE=C:\Users\logan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

"%PYTHON_EXE%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo PyInstaller is not installed for %PYTHON_EXE%.
  echo Install it with:
  echo "%PYTHON_EXE%" -m pip install pyinstaller
  popd
  exit /b 1
)

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --name LigenStudio --distpath dist --workpath build\pyinstaller --specpath build\pyinstaller scripts\launch_ligen_studio.py
popd
endlocal
