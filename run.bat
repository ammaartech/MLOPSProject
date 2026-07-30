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
REM
REM  Docker (no Python or venv needed — Docker Desktop must be running):
REM     run.bat docker-build    build the image
REM     run.bat docker-up       dashboard + MLflow UI, detached
REM     run.bat docker-down     stop them
REM     run.bat docker-pipeline all twelve stages inside a container
REM     run.bat docker-drift    drift monitor inside a container
REM     run.bat docker-push     push to Docker Hub (login required)
REM
REM  Kubernetes (three isolated environments, see k8s/README.md):
REM     run.bat k8s-up             deploy production + dev + qs
REM     run.bat k8s-down           remove all three
REM     run.bat k8s-status         pods, services and volumes
REM     run.bat k8s-seed [env]     copy this machine's database into an
REM                                environment so its dashboard has data
REM                                (default: all three)
REM     run.bat k8s-build <env>    dev: rebuild the image from the working
REM                                tree; qs: promote the dev image. Loads it
REM                                into the cluster and restarts that
REM                                environment ONLY.
REM     run.bat k8s-pipeline <env> run the twelve stages inside an
REM                                environment, against its own database
REM
REM  Each environment pins its own image tag (production :1.1.0, qs :qs,
REM  dev :dev) and owns its own volume, so a rebuild or a config edit in
REM  one cannot change what another one serves.
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
    echo   NOTE: dependency installation is managed by Docker -- reset-deps is a no-op.
    set "ACTION=setup"
)

REM ================================================================
REM  DOCKER ACTIONS
REM
REM  Dispatched BEFORE the Python prerequisites below. The entire point
REM  of the container is that it needs no interpreter, no venv and no
REM  pip install on this machine — checking for them first would defeat
REM  it on exactly the clean machine it is meant to serve.
REM ================================================================
set "IMAGE=ammaartech/predictive-resource-monitor"

if /i "%ACTION%"=="docker-build"    goto :docker_check
if /i "%ACTION%"=="docker-up"       goto :docker_check
if /i "%ACTION%"=="docker-down"     goto :docker_check
if /i "%ACTION%"=="docker-pipeline" goto :docker_check
if /i "%ACTION%"=="docker-drift"    goto :docker_check
if /i "%ACTION%"=="docker-push"     goto :docker_check
if /i "%ACTION%"=="k8s-up"          goto :k8s_check
if /i "%ACTION%"=="k8s-down"        goto :k8s_check
if /i "%ACTION%"=="k8s-status"      goto :k8s_check
if /i "%ACTION%"=="k8s-seed"        goto :k8s_check
if /i "%ACTION%"=="k8s-build"       goto :k8s_check
if /i "%ACTION%"=="k8s-pipeline"    goto :k8s_check
goto :prereqs

:docker_check
docker version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: cannot reach the Docker daemon.
    echo   Start Docker Desktop and wait for the whale icon to stop animating.
    exit /b 1
)

if /i "%ACTION%"=="docker-build"    goto :docker_build
if /i "%ACTION%"=="docker-up"       goto :docker_up
if /i "%ACTION%"=="docker-down"     goto :docker_down
if /i "%ACTION%"=="docker-pipeline" goto :docker_pipeline
if /i "%ACTION%"=="docker-drift"    goto :docker_drift
if /i "%ACTION%"=="docker-push"     goto :docker_push

:docker_build
docker compose build
goto :done

:docker_up
docker compose up -d dashboard mlflow
if errorlevel 1 exit /b 1
echo.
echo   dashboard : http://localhost:8501
echo   MLflow    : http://localhost:5000
echo   stop with : run.bat docker-down
goto :done

:docker_down
docker compose down
goto :done

:docker_pipeline
docker compose run --rm app pipeline
goto :done

:docker_drift
docker compose run --rm app drift
goto :done

:docker_push
REM Tag the current build with a version as well as latest, so a pushed
REM :latest can always be traced back to a specific immutable tag.
if "%ARG2%"=="" set "ARG2=1.0.0"
docker tag %IMAGE%:latest %IMAGE%:%ARG2%
if errorlevel 1 (
    echo   ERROR: no local image to tag. Run: run.bat docker-build
    exit /b 1
)
docker push %IMAGE%:%ARG2%
if errorlevel 1 exit /b 1
docker push %IMAGE%:latest
goto :done

REM ================================================================
REM  KUBERNETES ACTIONS
REM
REM  Three namespaces from one image: production, dev and qs, each with
REM  its own database, its own config and its own port. See k8s/README.md.
REM ================================================================
:k8s_check
kubectl version --client >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: kubectl was not found on PATH.
    echo   Docker Desktop ships one; enabling Kubernetes puts it there.
    exit /b 1
)
kubectl cluster-info --request-timeout=8s >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: no reachable Kubernetes cluster.
    echo   Docker Desktop -^> Settings -^> Kubernetes -^> Enable Kubernetes,
    echo   then wait for the indicator to turn green and retry.
    exit /b 1
)

if /i "%ACTION%"=="k8s-up"       goto :k8s_up
if /i "%ACTION%"=="k8s-down"     goto :k8s_down
if /i "%ACTION%"=="k8s-status"   goto :k8s_status
if /i "%ACTION%"=="k8s-seed"     goto :k8s_seed
if /i "%ACTION%"=="k8s-build"    goto :k8s_build
if /i "%ACTION%"=="k8s-pipeline" goto :k8s_pipeline

:k8s_up
call :k8s_ensure_images
if errorlevel 1 exit /b 1
for %%E in (production dev qs) do (
    echo [k8s] applying %%E ...
    kubectl apply -k "k8s\overlays\%%E"
    if errorlevel 1 exit /b 1
)
call :k8s_gateway_up
echo.
echo   production : http://localhost:30080   (image :1.1.0, 3 replicas)
echo   dev        : http://localhost:30081   (image :dev,   1 replica)
echo   qs         : http://localhost:30082   (image :qs,    2 replicas)
echo.
echo   Pods take up to a minute to pass their readiness probe.
echo   Watch with : run.bat k8s-status
echo.
echo   A fresh environment has an empty database, and the dashboard stops
echo   with "No successful ETL run" until it has one. Give it data with:
echo       run.bat k8s-seed          copy this machine's database in
echo       run.bat k8s-pipeline dev  or compute artifacts in place
goto :done

:k8s_down
for %%E in (production dev qs) do (
    echo [k8s] removing %%E ...
    kubectl delete -k "k8s\overlays\%%E" --ignore-not-found
)
echo [k8s] removing the NodePort gateway ...
docker rm -f rms-gateway >nul 2>&1
goto :done

:k8s_status
kubectl get pods,svc,pvc -A -l app=rms
echo.
echo   image and replicas per environment:
for %%E in (production dev qs) do (
    for /f "tokens=*" %%i in ('kubectl get deploy dashboard -n rms-%%E -o jsonpath^="{.spec.template.spec.containers[0].image} x{.status.readyReplicas}/{.spec.replicas}" 2^>nul') do echo     %%E : %%i
)
goto :done

REM ----------------------------------------------------------------
REM  k8s-seed — give an environment something to show.
REM
REM  Each namespace owns a separate volume, so a new one starts with an
REM  empty database and the dashboard calls st.stop() on "No successful
REM  ETL run". Seeding copies THIS machine's database in, once. From that
REM  moment the copies are independent: a config edit or a pipeline run
REM  in one cannot be seen by another.
REM ----------------------------------------------------------------
:k8s_seed
if "%ARG2%"=="" (
    for %%E in (production dev qs) do call :k8s_seed_one %%E
    goto :done
)
call :k8s_validate_env "%ARG2%"
if errorlevel 1 exit /b 1
call :k8s_seed_one "%ARG2%"
goto :done

REM ----------------------------------------------------------------
REM  k8s-build — rebuild ONE environment.
REM ----------------------------------------------------------------
:k8s_build
if "%ARG2%"=="" (
    echo   Usage: run.bat k8s-build ^<dev^|qs^>
    exit /b 1
)
if /i "%ARG2%"=="production" (
    echo.
    echo   Production is deliberately not built from the working tree.
    echo   It runs an immutable tag so that nothing you build can reach it
    echo   by accident. To ship a change, promote it: edit newTag in
    echo   k8s\overlays\production\kustomization.yaml to a version that has
    echo   passed qs, then run: run.bat k8s-up
    exit /b 1
)
if /i "%ARG2%"=="dev" goto :k8s_build_dev
if /i "%ARG2%"=="qs"  goto :k8s_build_qs
echo   ERROR: unknown environment "%ARG2%". Use dev or qs.
exit /b 1

:k8s_build_dev
echo [k8s] building the dev image from the working tree ...
docker compose build
if errorlevel 1 exit /b 1
docker tag %IMAGE%:latest %IMAGE%:dev
if errorlevel 1 exit /b 1
call :k8s_load_image dev
if errorlevel 1 exit /b 1
kubectl rollout restart deploy/dashboard -n rms-dev
echo.
echo   dev rebuilt at http://localhost:30081
echo   production and qs still run the images they were given.
goto :done

:k8s_build_qs
REM Promotion, not a build. qs must run the exact image dev produced, or
REM a pass here is not evidence about the thing that would be shipped.
echo [k8s] promoting the dev image to qs ...
docker tag %IMAGE%:dev %IMAGE%:qs
if errorlevel 1 (
    echo   ERROR: no %IMAGE%:dev to promote. Run: run.bat k8s-build dev
    exit /b 1
)
call :k8s_load_image qs
if errorlevel 1 exit /b 1
kubectl rollout restart deploy/dashboard -n rms-qs
echo.
echo   qs now runs the dev image, at http://localhost:30082
echo   production is untouched.
goto :done

REM ----------------------------------------------------------------
REM  k8s-pipeline — the twelve stages, inside one environment.
REM ----------------------------------------------------------------
:k8s_pipeline
if "%ARG2%"=="" (
    echo   Usage: run.bat k8s-pipeline ^<production^|dev^|qs^>
    exit /b 1
)
call :k8s_validate_env "%ARG2%"
if errorlevel 1 exit /b 1
echo [k8s] running the twelve stages inside rms-%ARG2% ...
REM Run inside the live pod on purpose: same image, same volume the
REM dashboard reads, so the artifacts written are exactly the ones that
REM environment will serve. This is the one writer; WAL lets the reading
REM replicas carry on while it works.
kubectl exec -n "rms-%ARG2%" deploy/dashboard -- python -m orchestration.run_pipeline
goto :done

REM ================================================================
REM  Kubernetes helpers — reached by CALL only.
REM ================================================================

REM ----------------------------------------------------------------
REM  Make the NodePorts reachable from Windows.
REM
REM  Docker Desktop's current Kubernetes runs the cluster inside a
REM  container ("desktop-control-plane"), and that container publishes
REM  ONE port to the host: 6443, the API server. So kubectl works
REM  perfectly while http://localhost:30080 refuses the connection —
REM  the Service is healthy, its endpoints are healthy, and nothing is
REM  listening on the Windows side of the boundary. Measured, not
REM  assumed: `docker port desktop-control-plane` lists only 6443/tcp,
REM  and a curl from a container on the cluster's own network gets 200
REM  from all three NodePorts.
REM
REM  LoadBalancer is not the way out — this cluster has no provisioner,
REM  so such a Service sits at <pending> forever (tried it).
REM
REM  This forwards the three ports from Windows to the node over the
REM  cluster's own Docker network. Traffic therefore arrives as real
REM  NodePort traffic and is load-balanced across the replicas by
REM  kube-proxy, with the Service's ClientIP affinity intact — which
REM  `kubectl port-forward` would not do, as it pins to a single pod
REM  and bypasses the Service entirely.
REM ----------------------------------------------------------------
:k8s_gateway_up
setlocal
set "K8SNODE="
for /f "tokens=*" %%n in ('kubectl get nodes -o jsonpath^="{.items[0].metadata.name}" 2^>nul') do set "K8SNODE=%%n"
if not defined K8SNODE (
    endlocal & exit /b 0
)
docker inspect "%K8SNODE%" >nul 2>&1
if errorlevel 1 (
    REM Not a containerised node — the older Docker Desktop cluster ran
    REM on the host's network and published NodePorts itself.
    endlocal & exit /b 0
)
docker inspect rms-gateway >nul 2>&1
if not errorlevel 1 (
    docker start rms-gateway >nul 2>&1
    echo [k8s] NodePort gateway already present.
    endlocal & exit /b 0
)
set "K8SNET="
for /f "tokens=*" %%m in ('docker inspect "%K8SNODE%" --format "{{.HostConfig.NetworkMode}}" 2^>nul') do set "K8SNET=%%m"
if not defined K8SNET (
    endlocal & exit /b 0
)
echo [k8s] starting the NodePort gateway on network %K8SNET% ...
docker run -d --name rms-gateway --network "%K8SNET%" --restart unless-stopped -p 30080:30080 -p 30081:30081 -p 30082:30082 --entrypoint sh alpine/socat:latest -c "socat tcp-listen:30080,fork,reuseaddr tcp-connect:%K8SNODE%:30080 & socat tcp-listen:30081,fork,reuseaddr tcp-connect:%K8SNODE%:30081 & socat tcp-listen:30082,fork,reuseaddr tcp-connect:%K8SNODE%:30082" >nul
if errorlevel 1 (
    echo   WARNING: the gateway did not start. The three environments are
    echo   running regardless — they are just not reachable from Windows.
    echo   Fall back to: kubectl port-forward -n rms-production svc/dashboard 30080:8501
)
endlocal & exit /b 0

:k8s_validate_env
if /i "%~1"=="production" exit /b 0
if /i "%~1"=="dev"        exit /b 0
if /i "%~1"=="qs"         exit /b 0
echo   ERROR: unknown environment "%~1". Use production, dev or qs.
exit /b 1

REM ----------------------------------------------------------------
REM  Make sure the tags the overlays ask for exist.
REM
REM  Only production runs a published tag. dev and qs run tags that exist
REM  purely to keep the environments apart, so on a machine that has never
REM  built them there is nothing to pull and the pods would sit in
REM  ErrImagePull. Create them from the release the first time.
REM ----------------------------------------------------------------
:k8s_ensure_images
for %%T in (dev qs) do (
    docker image inspect %IMAGE%:%%T >nul 2>&1
    if errorlevel 1 (
        echo [k8s] creating the :%%T tag from :1.1.0 ...
        docker tag %IMAGE%:1.1.0 %IMAGE%:%%T
        if errorlevel 1 (
            echo   ERROR: %IMAGE%:1.1.0 is not present locally.
            echo   Run "run.bat docker-build" first, or pull it.
            exit /b 1
        )
        call :k8s_load_image %%T
        if errorlevel 1 exit /b 1
    )
)
exit /b 0

REM ----------------------------------------------------------------
REM  Put a local image where the kubelet can actually see it.
REM
REM  Docker Desktop's current Kubernetes node is a container running its
REM  OWN containerd store. That store is NOT the Docker engine's: a tag
REM  `docker images` lists plainly can still fail with ErrImagePull,
REM  because the kubelet never looks there. Confirmed on this cluster —
REM  the engine held four tags while the node held one, pulled from
REM  Docker Hub. imagePullPolicy: IfNotPresent does not save you; "not
REM  present" is judged against the node's store.
REM
REM  Older Docker Desktop releases used a node that did share the store,
REM  so detect instead of assuming: if the node's name is also a running
REM  container, it is the kind-style node and needs the import.
REM ----------------------------------------------------------------
:k8s_load_image
setlocal
set "TAG=%~1"
set "K8SNODE="
for /f "tokens=*" %%n in ('kubectl get nodes -o jsonpath^="{.items[0].metadata.name}" 2^>nul') do set "K8SNODE=%%n"
if not defined K8SNODE (
    echo   ERROR: could not read the cluster's node name.
    endlocal & exit /b 1
)
docker inspect "%K8SNODE%" >nul 2>&1
if errorlevel 1 (
    REM Not a containerised node — engine and kubelet share one store.
    endlocal & exit /b 0
)
echo [k8s] loading %IMAGE%:%TAG% into node %K8SNODE% ...
docker save "%IMAGE%:%TAG%" -o "%TEMP%\rms-load-%TAG%.tar"
if errorlevel 1 (
    endlocal & exit /b 1
)
docker exec -i "%K8SNODE%" ctr -n k8s.io images import - < "%TEMP%\rms-load-%TAG%.tar"
set "RC=%ERRORLEVEL%"
del /q "%TEMP%\rms-load-%TAG%.tar" >nul 2>&1
endlocal & exit /b %RC%

REM ----------------------------------------------------------------
REM  Copy this machine's database into one environment's volume.
REM ----------------------------------------------------------------
:k8s_seed_one
setlocal
set "ENVNAME=%~1"
set "NS=rms-%ENVNAME%"
set "POD="

if not exist "dataset\metrics.db" (
    echo   ERROR: dataset\metrics.db not found. Collect on this machine first:
    echo       run.bat collect
    endlocal & exit /b 1
)

REM The "=" in the label selector is caret-escaped on purpose. Unescaped,
REM cmd's for /f parser mis-splits the command and the loop silently runs
REM nothing — POD comes back empty and this reads as "no pod deployed".
for /f "tokens=*" %%p in ('kubectl get pod -n %NS% -l component^=dashboard -o jsonpath^="{.items[0].metadata.name}" 2^>nul') do set "POD=%%p"
if not defined POD (
    echo   [%ENVNAME%] no dashboard pod running — deploy first: run.bat k8s-up
    endlocal & exit /b 1
)

echo [k8s] seeding %NS% from dataset\metrics.db ...
REM Land it under a temporary name. The running dashboard holds the live
REM database open, and writing straight over it would be read half-copied.
kubectl cp "dataset\metrics.db" "%NS%/%POD%:/app/data/seed.db"
if errorlevel 1 (
    endlocal & exit /b 1
)
REM Move into place in one step, and delete the sidecars first: a stale
REM metrics.db-wal belongs to the file being replaced, and SQLite would
REM cheerfully replay it over the new one.
kubectl exec -n %NS% "%POD%" -- sh -c "rm -f /app/data/metrics.db-wal /app/data/metrics.db-shm && mv -f /app/data/seed.db /app/data/metrics.db"
if errorlevel 1 (
    endlocal & exit /b 1
)
if exist "data\models" kubectl cp "data\models" "%NS%/%POD%:/app/data" >nul 2>&1
REM Restart so every replica reopens the new file — and drops the config
REM values it memoised from the old one, which nothing else invalidates.
kubectl rollout restart deploy/dashboard -n %NS% >nul 2>&1
echo   [%ENVNAME%] seeded; pods restarting
endlocal & exit /b 0

:prereqs

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
REM  The stamp records requirements.txt's timestamp and size, not a bare
REM  "installed" flag. Adding a line to requirements.txt therefore
REM  invalidates the stamp and forces a reinstall. The flag version left
REM  every venv created before a new dependency permanently one package
REM  short, and the failure surfaced far from the cause — as a
REM  ModuleNotFoundError on streamlit_autorefresh inside the dashboard.
REM ================================================================
set "REQ_FINGERPRINT="
for %%F in ("requirements.txt") do set "REQ_FINGERPRINT=%%~tF %%~zF"

set "DEPS_FINGERPRINT="
if exist "%DEPS_STAMP%" set /p DEPS_FINGERPRINT=<"%DEPS_STAMP%"

if not "%DEPS_FINGERPRINT%"=="%REQ_FINGERPRINT%" (
    echo.
    echo [prereq] installing dependencies from requirements.txt ...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   ERROR: dependency installation failed.
        exit /b 1
    )
    REM  Redirect first: the fingerprint ends in the byte count, and a
    REM  digit immediately before ">" is parsed as a stream handle.
    >"%DEPS_STAMP%" echo %REQ_FINGERPRINT%
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
echo Docker: docker-build ^| docker-up ^| docker-down ^| docker-pipeline ^| docker-drift ^| docker-push
echo K8s:    k8s-up ^| k8s-down ^| k8s-status ^| k8s-seed ^| k8s-build ^| k8s-pipeline
exit /b 1

REM ----------------------------------------------------------------
:interactive
echo ======================================================================
echo   PREDICTIVE RESOURCE MONITORING SYSTEM
echo ======================================================================
echo.
echo   1  Data entry ^& logging (Collect metrics + Pipeline cycle)
echo   2  Dashboard (Streamlit)
echo.
echo   0  Exit
echo.
set /p "CHOICE=Select: "
if "%CHOICE%"=="1" goto :schedule
if "%CHOICE%"=="2" goto :dashboard
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
:docker_build_and_up
docker version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Docker daemon is not running.
    echo   Open Docker Desktop from the Start menu and wait for the whale
    echo   icon in the system tray to stop animating, then try again.
    goto :interactive
)
echo Building image...
docker compose build
if errorlevel 1 goto :done
echo Starting dashboard + MLflow...
docker compose up -d dashboard mlflow
if errorlevel 1 goto :done
echo.
echo   Dashboard : http://localhost:8501
echo   MLflow    : http://localhost:5000
echo   Stop with : select X from this menu
goto :done

:docker_up_only
docker version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Docker daemon is not running.
    echo   Open Docker Desktop and wait for the whale icon to stop animating.
    goto :interactive
)
docker compose up -d dashboard mlflow
if errorlevel 1 goto :done
echo.
echo   Dashboard : http://localhost:8501
echo   MLflow    : http://localhost:5000
goto :done

:docker_down_menu
docker version >nul 2>&1
if errorlevel 1 (
    echo   ERROR: Docker daemon is not running.
    goto :interactive
)
docker compose down
goto :done

:docker_pipeline_menu
docker version >nul 2>&1
if errorlevel 1 (
    echo   ERROR: Docker daemon is not running.
    goto :interactive
)
docker compose run --rm app pipeline
goto :done

REM ----------------------------------------------------------------
:done
endlocal
exit /b 0
