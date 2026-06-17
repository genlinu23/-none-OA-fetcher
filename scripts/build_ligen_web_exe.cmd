@echo off
setlocal
pushd "%~dp0.."

set "PYTHON_EXE=%~1"
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=python"

where npm >nul 2>nul
if errorlevel 1 (
  echo npm was not found. Install Node.js or run from an environment that has npm.
  popd
  exit /b 1
)

call "%PYTHON_EXE%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo PyInstaller is not installed for %PYTHON_EXE%.
  echo Install it with:
  echo "%PYTHON_EXE%" -m pip install pyinstaller
  popd
  exit /b 1
)

echo Building React frontend...
pushd ligen_downloader\web_frontend
call npm run build
if errorlevel 1 (
  popd
  popd
  exit /b 1
)
popd

echo Building distributable exe...
call "%PYTHON_EXE%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --noupx ^
  --onefile ^
  --windowed ^
  --name PKU-Literature-Workbench ^
  --distpath dist ^
  --workpath build\pyinstaller ^
  --specpath build\pyinstaller ^
  --add-data "%CD%\ligen_downloader\web_frontend\dist;ligen_downloader\web_frontend\dist" ^
  --hidden-import scripts.run_ligen_script_mode ^
  --hidden-import scripts.run_ligen_paper_download_multiport ^
  --hidden-import scripts.run_ligen_paper_download ^
  --hidden-import scripts.open_publisher_warmup_tabs ^
  --hidden-import scripts.fetch_publisher_pdfs ^
  --hidden-import websocket ^
  scripts\launch_ligen_web_dist.py
if errorlevel 1 (
  popd
  exit /b 1
)

echo.
echo Built: %CD%\dist\PKU-Literature-Workbench.exe
popd
endlocal
