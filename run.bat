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
REM     run.bat                 ensure Kubernetes is up, deploy/refresh
REM                             production + dev + qs, sync the Supabase
REM                             login secret into all three, then open
REM                             ONLY the production dashboard
REM                             (http://localhost:30080) before showing
REM                             the local menu. dev/qs stay reachable at
REM                             :30081 / :30082 but are not auto-opened.
REM                             The local venv prerequisites still run
REM                             first, because the menu's CRUD/logger/
REM                             refresh/scheduler options work against
REM                             this machine's own dataset\metrics.db,
REM                             independently of what is served in k8s.
REM     run.bat refresh         one pipeline pass on demand: ETL,
REM                             features, forecast, recommendation. Use
REM                             when the loop is not running.
REM     run.bat setup           prerequisites only, then exit
REM     run.bat crud            the Data / CRUD submenu (menu option 1)
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
call :k8s_sync_secrets
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
    echo   running regardless - they are just not reachable from Windows.
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
REM  Sync the Supabase login credentials into all three namespaces.
REM
REM  dashboard/auth.py reads SUPABASE_URL and SUPABASE_ANON_KEY from the
REM  environment, the same bootstrap-exception pattern as DB_PATH: a
REM  value needed before any database (or any login) is reachable, so it
REM  cannot itself live in the config table. Locally that comes from
REM  .env; in the cluster it has to arrive as a Kubernetes Secret, since
REM  baking it into the image would ship it inside a public Docker Hub
REM  pull (see .dockerignore) and a plain env value in deployment.yaml
REM  would commit it to git.
REM
REM  This is the ONLY place these two values are read out of .env for
REM  Kubernetes — one source of truth, kept in sync on every k8s-up
REM  rather than typed once and left to drift. Re-running is always
REM  safe: `kubectl apply` on a regenerated Secret updates in place.
REM
REM  Deliberately does NOT sync SUPABASE_DB_URL — that connection string
REM  is a Postgres superuser credential, read only by
REM  `python -m dashboard.apply_sql` on this machine, and the running
REM  dashboard has no legitimate use for it. Handing the pods a
REM  credential they do not need would widen the blast radius of a
REM  compromised pod for no reason.
REM ----------------------------------------------------------------
:k8s_sync_secrets
if not exist ".env" (
    echo   WARNING: .env not found — Supabase login will show
    echo   "not configured" in every environment until you create one
    echo   ^(copy .env.example to .env^) and run this again.
    exit /b 0
)
set "SB_URL="
set "SB_ANON="
for /f "tokens=1,* delims==" %%a in ('findstr /b "SUPABASE_URL=" .env') do set "SB_URL=%%b"
for /f "tokens=1,* delims==" %%a in ('findstr /b "SUPABASE_ANON_KEY=" .env') do set "SB_ANON=%%b"
if not defined SB_URL (
    echo   WARNING: SUPABASE_URL not set in .env — skipping secret sync.
    exit /b 0
)
if not defined SB_ANON (
    echo   WARNING: SUPABASE_ANON_KEY not set in .env — skipping secret sync.
    exit /b 0
)
for %%E in (production dev qs) do (
    kubectl create namespace rms-%%E --dry-run=client -o yaml 2>nul | kubectl apply -f - >nul 2>&1
    kubectl create secret generic rms-supabase -n rms-%%E ^
        --from-literal=SUPABASE_URL=%SB_URL% ^
        --from-literal=SUPABASE_ANON_KEY=%SB_ANON% ^
        --dry-run=client -o yaml | kubectl apply -f - >nul
    echo   [k8s] synced Supabase login into rms-%%E
)
exit /b 0

REM ----------------------------------------------------------------
REM  Bring Kubernetes up (if needed), deploy all three environments,
REM  and open ONLY production in the browser. Called from :autostart,
REM  which is why every failure path falls through to the local menu
REM  instead of exiting run.bat -- CRUD/logger/refresh do not need k8s.
REM ----------------------------------------------------------------
:k8s_autostart
docker version >nul 2>&1
if not errorlevel 1 goto :k8s_autostart_docker_ready
echo [k8s] Docker Desktop is not running -- starting it ...
docker desktop start >nul 2>&1
set "DOCKER_WAIT=0"

:k8s_autostart_wait_docker
docker version >nul 2>&1
if not errorlevel 1 goto :k8s_autostart_docker_ready
set /a DOCKER_WAIT+=1
if %DOCKER_WAIT% GEQ 24 goto :k8s_autostart_docker_timeout
timeout /t 5 >nul
goto :k8s_autostart_wait_docker

:k8s_autostart_docker_timeout
echo   Docker Desktop did not come up within 2 minutes.
echo   Start it manually, then run.bat again for production to
echo   open automatically. Continuing with the local menu only.
goto :k8s_autostart_skip

:k8s_autostart_docker_ready
echo   Docker Desktop is up.

kubectl cluster-info --request-timeout=5s >nul 2>&1
if not errorlevel 1 goto :k8s_autostart_cluster_ready
echo [k8s] Kubernetes is not reachable -- waiting for it to start ...
echo   ^(If this is the first time, enable it once: Docker Desktop -^>
echo   Settings -^> Kubernetes -^> Enable Kubernetes -^> Apply ^& Restart.^)
set "K8S_WAIT=0"

:k8s_autostart_wait_cluster
kubectl cluster-info --request-timeout=5s >nul 2>&1
if not errorlevel 1 goto :k8s_autostart_cluster_ready
set /a K8S_WAIT+=1
if %K8S_WAIT% GEQ 24 goto :k8s_autostart_cluster_timeout
timeout /t 5 >nul
goto :k8s_autostart_wait_cluster

:k8s_autostart_cluster_timeout
echo   Kubernetes did not come up within 2 minutes.
echo   Check Docker Desktop -^> Settings -^> Kubernetes, then
echo   run.bat again. Continuing with the local menu only.
goto :k8s_autostart_skip

:k8s_autostart_cluster_ready
echo   Kubernetes is up.

echo [k8s] deploying production, dev and qs ...
call :k8s_ensure_images
if errorlevel 1 goto :k8s_autostart_skip
call :k8s_sync_secrets
for %%E in (production dev qs) do (
    kubectl apply -k "k8s\overlays\%%E" >nul
    if errorlevel 1 (
        echo   ERROR: failed to apply %%E. Continuing with the local menu only.
        goto :k8s_autostart_skip
    )
)
call :k8s_gateway_up

echo [k8s] waiting for production to become ready ...
kubectl wait --for=condition=Ready pod -l app=rms,component=dashboard -n rms-production --timeout=120s >nul 2>&1

echo.
echo   production : http://localhost:30080   (opening now)
echo   dev        : http://localhost:30081   (not opened automatically)
echo   qs         : http://localhost:30082   (not opened automatically)
echo.
start "" "http://localhost:30080"
goto :k8s_autostart_done

:k8s_autostart_skip
echo.
echo   Kubernetes was not brought up. Deploy manually later with:
echo       run.bat k8s-up
echo.

:k8s_autostart_done
exit /b 0

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
    echo   [%ENVNAME%] no dashboard pod running - deploy first: run.bat k8s-up
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
if /i "%ACTION%"=="crud"      goto :crud
if /i "%ACTION%"=="collect"   goto :collect
if /i "%ACTION%"=="load"      goto :load
if /i "%ACTION%"=="pipeline"  goto :pipeline
if /i "%ACTION%"=="refresh"   goto :refresh
if /i "%ACTION%"=="drift"     goto :drift
if /i "%ACTION%"=="schedule"  goto :schedule
if /i "%ACTION%"=="dashboard" goto :dashboard
if /i "%ACTION%"=="menu"      goto :pymenu
if /i "%ACTION%"=="mlflow"    goto :mlflow
if /i "%ACTION%"=="menu-interactive" goto :autostart

echo Unknown action "%ACTION%".
echo Valid: setup ^| crud ^| collect ^| load ^| pipeline ^| drift ^| schedule ^| dashboard ^| menu ^| mlflow ^| reset-deps
echo Docker: docker-build ^| docker-up ^| docker-down ^| docker-pipeline ^| docker-drift ^| docker-push
echo K8s:    k8s-up ^| k8s-down ^| k8s-status ^| k8s-seed ^| k8s-build ^| k8s-pipeline
exit /b 1

REM ----------------------------------------------------------------
REM  Autostart -- Kubernetes, not the local Streamlit process.
REM
REM  Production is the one dashboard this opens automatically, and it is
REM  opened as the Kubernetes Service, not `streamlit run` on 8501 --
REM  8501 has no login gate of its own to guarantee and no replica count,
REM  it is just this one process. dev and qs are deployed and reachable
REM  (:30081 / :30082) but never auto-opened, so nobody mistakes one of
REM  them for the environment that matters.
REM
REM  The scheduler (the continuous collect->forecast->recommend loop) is
REM  still not started here -- see menu option 4. It operates on this
REM  machine's own dataset\metrics.db, independently of what k8s serves.
REM
REM  Best-effort throughout: a machine with Docker Desktop closed, or
REM  Kubernetes never enabled, still gets the local menu -- it just skips
REM  straight to :interactive with a clear note instead of failing run.bat
REM  entirely, since CRUD/logger/refresh have nothing to do with k8s.
REM ----------------------------------------------------------------
:autostart
echo.
echo Starting the live system...
echo.
call :k8s_autostart

REM ----------------------------------------------------------------
:interactive
echo ======================================================================
echo   PREDICTIVE RESOURCE MONITORING SYSTEM
echo ======================================================================
echo.
echo   Production  http://localhost:30080     (opened automatically)
echo   Dev         http://localhost:30081     (open manually if needed)
echo   QS          http://localhost:30082     (open manually if needed)
echo   Scheduler   not running -- start it with option 4 below
echo.
echo   1  Data / CRUD operations (view, create, edit, delete)
echo   2  Logger (extra sampling options)
echo   3  Refresh forecasts now (one pipeline pass)
echo   4  Scheduler (continuous loop, or a fixed number of cycles)
echo.
echo   0  Exit
echo.
REM  The dashboard is deliberately not a menu entry. Streamlit holds the
REM  console for as long as it serves, so launching it from here ended the
REM  session that launched it -- the menu only came back when the UI was
REM  killed. It is a long-running service, so it is started above, in its
REM  own window. `run.bat dashboard` still runs one in the foreground.
REM
REM  The scheduler is NOT started automatically -- option 4 is the only
REM  way it starts. If a second one is launched while one is already
REM  running, its PID lock in data\scheduler.lock makes the redundant one
REM  print why it is stopping and exit, rather than double-sampling.
set /p "CHOICE=Select: "
if "%CHOICE%"=="1" goto :crud
if "%CHOICE%"=="2" goto :logger_menu
if "%CHOICE%"=="3" goto :refresh_once
if "%CHOICE%"=="4" goto :scheduler_menu
if "%CHOICE%"=="0" goto :done
echo Invalid selection.
goto :interactive

REM ----------------------------------------------------------------
REM  Logger submenu.
REM
REM  The interval is the gap BETWEEN samples; the duration is how long to
REM  keep sampling. Blank at either prompt takes the default - the
REM  configured collector.sample_interval_sec, and no time limit - so the
REM  submenu never forces a number on anyone who just wants it running.
REM
REM  Values are passed straight to argparse, which rejects a typo with a
REM  message and returns here rather than acting on a garbage number.
REM ----------------------------------------------------------------
:logger_menu
echo.
echo ----------------------------------------------------------------------
echo   LOGGER
echo ----------------------------------------------------------------------
echo.
echo   Every sample is written to the database and mirrored to
echo   data\exports\metrics_raw.csv as it is taken.
echo.
echo   1  Log one sample now
echo   2  Log for a fixed duration
echo   3  Log continuously (Ctrl+C to stop)
echo.
echo   0  Back
echo.
set /p "CHOICE=Select: "
if "%CHOICE%"=="1" (
    echo.
    "%VENV_PY%" -m collector.psutil_logger --once
    echo.
    pause
    goto :logger_menu
)
if "%CHOICE%"=="2" goto :logger_fixed
if "%CHOICE%"=="3" (
    echo.
    echo Logging continuously -- press Ctrl+C to stop.
    "%VENV_PY%" -m collector.psutil_logger
    echo.
    pause
    goto :logger_menu
)
if "%CHOICE%"=="0" goto :interactive
echo Invalid selection.
goto :logger_menu

:logger_fixed
set "LOGMINS="
set "LOGEVERY="
set /p "LOGMINS=  Minutes to log for: "
set /p "LOGEVERY=  Seconds between samples [configured default]: "
if not defined LOGMINS (
    echo   Cancelled.
    pause
    goto :logger_menu
)
echo.
if defined LOGEVERY (
    "%VENV_PY%" -m collector.psutil_logger --minutes %LOGMINS% --interval %LOGEVERY%
) else (
    "%VENV_PY%" -m collector.psutil_logger --minutes %LOGMINS%
)
echo.
pause
goto :logger_menu

REM ----------------------------------------------------------------
REM  One pipeline pass, then back to the menu. The dashboard's sidebar
REM  button runs exactly this, detached; this is the console version.
REM ----------------------------------------------------------------
:refresh_once
echo.
"%VENV_PY%" -m orchestration.refresh
echo.
pause
goto :interactive

REM ----------------------------------------------------------------
REM  Scheduler submenu.
REM
REM  This is the "continuously analyses" clause of the brief: a loop that
REM  re-runs the pipeline on a cadence and checks for drift every so many
REM  cycles. --with-collector samples in the same process, so one window
REM  both gathers data and acts on it.
REM ----------------------------------------------------------------
:scheduler_menu
echo.
echo ----------------------------------------------------------------------
echo   SCHEDULER
echo ----------------------------------------------------------------------
echo.
echo   1  Run continuously (Ctrl+C to stop)
echo   2  Run continuously, collecting in the same process
echo   3  Run a fixed number of cycles
echo.
echo   0  Back
echo.
set /p "CHOICE=Select: "
if "%CHOICE%"=="1" (
    echo.
    echo Scheduler running -- press Ctrl+C to stop.
    "%VENV_PY%" -m orchestration.scheduler --drift-every 10
    echo.
    pause
    goto :scheduler_menu
)
if "%CHOICE%"=="2" (
    echo.
    echo Scheduler + collector running -- press Ctrl+C to stop.
    "%VENV_PY%" -m orchestration.scheduler --drift-every 10 --with-collector
    echo.
    pause
    goto :scheduler_menu
)
if "%CHOICE%"=="3" goto :scheduler_cycles
if "%CHOICE%"=="0" goto :interactive
echo Invalid selection.
goto :scheduler_menu

:scheduler_cycles
set "SCHEDCYCLES="
set "SCHEDEVERY="
set /p "SCHEDCYCLES=  Number of cycles: "
set /p "SCHEDEVERY=  Seconds between cycles [configured default]: "
if not defined SCHEDCYCLES (
    echo   Cancelled.
    pause
    goto :scheduler_menu
)
echo.
if defined SCHEDEVERY (
    "%VENV_PY%" -m orchestration.scheduler --cycles %SCHEDCYCLES% --interval %SCHEDEVERY%
) else (
    "%VENV_PY%" -m orchestration.scheduler --cycles %SCHEDCYCLES%
)
echo.
pause
goto :scheduler_menu

REM ----------------------------------------------------------------
REM  CRUD submenu.
REM
REM  Each entry runs ONE operation from crud\console.py and comes back
REM  here. The operation names below are that module's own dispatch keys,
REM  so this menu and `python -m crud.console <op>` cannot drift apart.
REM
REM  batch asks only WHICH operation. Record ids, field names and values
REM  are prompted for in Python, where a typo can be rejected and retried
REM  — `set /p` would hand an unvalidated string straight to SQL and a
REM  mistyped number would end the session.
REM ----------------------------------------------------------------
:crud
echo.
echo ----------------------------------------------------------------------
echo   DATA / CRUD OPERATIONS
echo ----------------------------------------------------------------------
echo.
echo   Create, read, update and delete the logged metric values.
echo   Every write is mirrored to data\exports\metrics_raw.csv as it happens.
echo.
echo   1  View all records
echo   2  View latest N records
echo   3  View records between two timestamps
echo   4  Total record count
echo   5  Create a record
echo   6  Update a field on a record
echo   7  Delete a record
echo   8  Purge records before a timestamp
echo.
echo   0  Back
echo.
set "CRUDOP="
set /p "CHOICE=Select: "
if "%CHOICE%"=="1" set "CRUDOP=view"
if "%CHOICE%"=="2" set "CRUDOP=latest"
if "%CHOICE%"=="3" set "CRUDOP=between"
if "%CHOICE%"=="4" set "CRUDOP=count"
if "%CHOICE%"=="5" set "CRUDOP=create"
if "%CHOICE%"=="6" set "CRUDOP=update"
if "%CHOICE%"=="7" set "CRUDOP=delete"
if "%CHOICE%"=="8" set "CRUDOP=purge"

if defined CRUDOP (
    echo.
    "%VENV_PY%" -m crud.console %CRUDOP%
    echo.
    pause
    goto :crud
)
if "%CHOICE%"=="0" goto :interactive

echo Invalid selection.
goto :crud

REM ----------------------------------------------------------------
:collect
REM The collector runs until Ctrl+C. Pair it with the load generator in a
REM second window, or the CPU series is a flat idle line with nothing to
REM forecast.
echo Collecting metrics - press Ctrl+C to stop.
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

:refresh
REM One pass: ETL, features, forecast, recommendation. What the background
REM scheduler used to do on a timer, now done when asked.
"%VENV_PY%" -m orchestration.refresh
goto :done

:drift
"%VENV_PY%" -m serving.drift
goto :done

:schedule
echo Continuous loop - press Ctrl+C to stop.
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
