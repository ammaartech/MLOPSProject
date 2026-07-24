@echo off
REM ======================================================================
REM  Predictive Resource Monitoring System — Windows launcher
REM
REM  Handles every prerequisite before running anything:
REM     1. Python on PATH
REM     2. the .venv virtual environment
REM     3. pip dependencies from requirements.txt
REM     4. data directories and database initialisation (tables + config seed)
REM
REM  Usage:
REM     run.bat                 interactive menu
REM     run.bat setup           prerequisites only, then exit
REM     run.bat collect [mins]  sample this machine into the database
REM     run.bat load [mins]     run the CPU load generator
REM     run.bat pipeline        all twelve stages, one pass
REM     run.bat drift           drift monitor + auto-retrain
REM     run.bat schedule        the continuous loop (Ctrl+C to stop)
REM     run.bat dashboard       Streamlit UI
REM     run.bat menu            the Python menu in main.py
REM     run.bat mlflow          MLflow tracking UI
REM     run.bat reset-deps      force a clean dependency reinstall
REM
REM  Every Python entry point is invoked as a MODULE (-m, dotted, no .py).
REM  Running `python model\forecast.py` breaks the package imports.
REM ======================================================================

setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "DEPS_STAMP=%VENV_DIR%\.deps_ok"

REM ---------------------------------------------------------------- args
set "ACTION=%~1"
set "ARG2=%~2"
if "%ACTION%"=="" set "ACTION=menu-interactive"

if /i "%ACTION%"=="reset-deps" (
    if exist "%DEPS_STAMP%" del /q "%DEPS_STAMP%"
    set "ACTION=setup"
)

REM ================================================================
REM  PREREQUISITE 1 — Python interpreter
REM ================================================================
echo.
echo [prereq] checking Python...
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Python was not found on PATH.
    echo   Install Python 3.11 or newer from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during installation.
    echo.
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo   found Python %PYVER%

REM ================================================================
REM  PREREQUISITE 2 — virtual environment
REM ================================================================
if not exist "%VENV_PY%" (
    echo.
    echo [prereq] creating virtual environment in %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo   ERROR: could not create the virtual environment.
        echo   Try: python -m pip install --user virtualenv
        exit /b 1
    )
    REM A newly created venv has no dependencies, whatever a stale stamp says.
    if exist "%DEPS_STAMP%" del /q "%DEPS_STAMP%"
    echo   created.
) else (
    echo [prereq] virtual environment present.
)

REM ================================================================
REM  PREREQUISITE 3 — dependencies
REM
REM  The stamp file records that requirements.txt was installed
REM  successfully. Deleting it (or `run.bat reset-deps`) forces a
REM  reinstall; otherwise startup stays fast.
REM ================================================================
if not exist "%DEPS_STAMP%" (
    echo.
    echo [prereq] installing dependencies from requirements.txt ...
    echo          ^(first run only — this takes a few minutes^)
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   ERROR: dependency installation failed. Read the pip output above.
        echo   Common cause: no internet connection, or a Python version that
        echo   has no wheels for the pinned scikit-learn / numpy builds.
        exit /b 1
    )
    echo installed > "%DEPS_STAMP%"
    echo   dependencies installed.
) else (
    echo [prereq] dependencies already installed.
)

REM ================================================================
REM  PREREQUISITE 4 — database: create tables, seed default config,
REM  apply column migrations. Idempotent, safe on every launch.
REM ================================================================
echo [prereq] initialising database...
"%VENV_PY%" -c "from database.connection import get_connection; c = get_connection(); print('   database ready at', __import__('config').DB_PATH) if c else exit(1); c.close()"
if errorlevel 1 (
    echo   ERROR: database initialisation failed.
    exit /b 1
)

echo.
echo [prereq] all prerequisites satisfied.
echo.

REM ================================================================
REM  ACTIONS
REM ================================================================
if /i "%ACTION%"=="setup"     goto :done
if /i "%ACTION%"=="collect"   goto :collect
if /i "%ACTION%"=="load"      goto :load
if /i "%ACTION%"=="pipeline"  goto :pipeline
if /i "%ACTION%"=="drift"     goto :drift
if /i "%ACTION%"=="schedule"  goto :schedule
if /i "%ACTION%"=="dashboard" goto :dashboard
if /i "%ACTION%"=="menu"      goto :pymenu
if /i "%ACTION%"=="mlflow"    goto :mlflow
if /i "%ACTION%"=="menu-interactive" goto :interactive

echo Unknown action "%ACTION%".
echo Valid: setup ^| collect ^| load ^| pipeline ^| drift ^| schedule ^| dashboard ^| menu ^| mlflow ^| reset-deps
exit /b 1

REM ----------------------------------------------------------------
:interactive
echo ======================================================================
echo   PREDICTIVE RESOURCE MONITORING SYSTEM
echo ======================================================================
echo.
echo   1  Collect metrics          (sample this machine)
echo   2  Run load generator       (makes CPU forecastable)
echo   3  Run full pipeline        (all 12 stages)
echo   4  Drift monitor + retrain
echo   5  Continuous scheduler
echo   6  Dashboard (Streamlit)
echo   7  Python menu (main.py)
echo   8  MLflow UI
echo   0  Exit
echo.
set /p "CHOICE=Select: "
if "%CHOICE%"=="1" goto :collect
if "%CHOICE%"=="2" goto :load
if "%CHOICE%"=="3" goto :pipeline
if "%CHOICE%"=="4" goto :drift
if "%CHOICE%"=="5" goto :schedule
if "%CHOICE%"=="6" goto :dashboard
if "%CHOICE%"=="7" goto :pymenu
if "%CHOICE%"=="8" goto :mlflow
if "%CHOICE%"=="0" goto :done
echo Invalid selection.
goto :interactive

REM ----------------------------------------------------------------
:collect
REM The collector runs until Ctrl+C. Pair it with the load generator in a
REM second window, or the CPU series is a flat idle line with nothing to
REM forecast.
echo Collecting metrics — press Ctrl+C to stop.
"%VENV_PY%" -m collector.psutil_logger
goto :done

:load
REM Must be launched as a module: multiprocessing on Windows re-imports
REM this module to spawn the burn workers, and any other launch form
REM forks uncontrollably.
if "%ARG2%"=="" set "ARG2=15"
echo Running the load generator for %ARG2% minute(s)...
"%VENV_PY%" -m collector.load_generator --minutes %ARG2%
goto :done

:pipeline
"%VENV_PY%" -m orchestration.run_pipeline
goto :done

:drift
"%VENV_PY%" -m serving.drift
goto :done

:schedule
echo Continuous loop — press Ctrl+C to stop.
"%VENV_PY%" -m orchestration.scheduler --with-collector
goto :done

:dashboard
REM Streamlit is the one exception to the -m module rule: it takes a path.
echo Starting the dashboard at http://localhost:8501 ...
"%VENV_DIR%\Scripts\streamlit.exe" run dashboard\app.py
goto :done

:pymenu
"%VENV_PY%" main.py
goto :done

:mlflow
echo Starting MLflow UI at http://localhost:5000 ...
"%VENV_DIR%\Scripts\mlflow.exe" ui --backend-store-uri sqlite:///data/mlflow.db
goto :done

REM ----------------------------------------------------------------
:done
endlocal
exit /b 0
