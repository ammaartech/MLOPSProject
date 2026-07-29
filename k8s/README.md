# Kubernetes — three environments that cannot reach each other

Production, dev and QS. Each is a namespace with its own database, its own
configuration, its own image tag, its own port and its own replica count.
The isolation is the point: a rebuild or a config edit in one environment
must not change what another one serves.

| Environment | Namespace        | URL                    | Image tag | Replicas | CPU request | Memory request |
|-------------|------------------|------------------------|-----------|----------|-------------|----------------|
| production  | `rms-production` | http://localhost:30080 | `:1.1.0`  | 3        | 500m        | 1Gi            |
| dev         | `rms-dev`        | http://localhost:30081 | `:dev`    | 1        | 100m        | 256Mi          |
| qs          | `rms-qs`         | http://localhost:30082 | `:qs`     | 2        | 500m        | 1Gi            |

---

## Prerequisite

Docker Desktop → Settings → Kubernetes → **Enable Kubernetes** → Apply &
Restart. When it is ready:

```powershell
kubectl get nodes          # one node, STATUS Ready
```

## Deploy

```powershell
run.bat k8s-up             # all three, plus the gateway below
run.bat k8s-seed           # give them data (see the next section)
run.bat k8s-status         # pods, services, volumes, and the image each runs
run.bat k8s-down           # remove everything
```

---

## Why a new port looks empty

A fresh environment gets a fresh volume, so its database has the schema
and the 74 seeded config rows and nothing else. `dashboard/app.py` opens
with

```python
run = latest_run()
if run is None:
    st.error("No successful ETL run. Run `python -m orchestration.run_pipeline` first.")
    st.stop()
```

so the pod is healthy, the Service has endpoints, the readiness probe
passes — and the page stops on an error before drawing anything. The port
is not empty; the environment has nothing to show yet.

Two ways to fix it, and they mean different things:

```powershell
run.bat k8s-seed dev          # copy THIS machine's database in, once
run.bat k8s-pipeline dev      # compute artifacts in place, from its own data
```

Seeding is a snapshot restore: it copies `dataset/metrics.db` — the
repository's tracked, collected database — into the namespace, where each
gets its **own copy** on its **own volume**, and from that moment they
diverge independently. It is how a staging environment is normally
refreshed from production, and it does not weaken the isolation below.

Note the pods never read `dataset/` directly. The deployment sets
`RESOURCE_MONITOR_DB_DIR=/app/data`, so every environment's live database
is on its own claim; the tracked file is a seed, not a shared mount.
Docker Compose deliberately does the opposite — see the root README.

---

## The three ways an environment is isolated

### 1. Its own data and configuration

One PVC per namespace. Configuration in this project is a table in the
database, so a separate volume is what makes the environments genuinely
separate rather than cosmetic. Verified by changing one value in dev and
reading the config fingerprint — which the dashboard shows in its sidebar
— in all three:

```
                 before                          after (dev edit only)
rms-production   fp 2c28ed14bcda  headroom 0.2    fp 2c28ed14bcda  headroom 0.2
rms-dev          fp 2c28ed14bcda  headroom 0.2    fp 24c33f2a0562  headroom 0.45
rms-qs           fp 2c28ed14bcda  headroom 0.2    fp 2c28ed14bcda  headroom 0.2
```

A footnote worth knowing before using the fingerprint as evidence: it
hashes the stored **text**, so setting `0.20` to `0.2` changes it while
changing nothing numerically.

### 2. Its own image tag

This is the one that used to leak. Every overlay named `:1.1.0`, the pull
policy is `IfNotPresent`, and rebuilding that tag to try something in dev
silently rewrote the image production would start from — landing on the
next pod restart, with no deploy and no trace.

Now production pins an immutable `:1.1.0`, dev owns a mutable `:dev`, and
qs owns `:qs`. Rebuilding one cannot reach the others:

```powershell
run.bat k8s-build dev      # build from the working tree -> :dev -> restart dev
run.bat k8s-build qs       # promote the dev image       -> :qs  -> restart qs
```

`k8s-build qs` retags rather than compiling, on purpose: qs must run the
exact image dev produced, or a pass there is not evidence about the thing
that would ship. Production has no build verb at all — you promote it by
editing `newTag` in `overlays/production/kustomization.yaml` to a version
that has passed qs.

Verified by rebuilding dev with a change and grepping the running code in
each namespace:

```
rms-production   0     # untouched
rms-qs           0     # untouched
rms-dev          2     # has the change
```

### 3. Its own port

Separate NodePorts, reached through the gateway described below.

---

## Two things about this cluster that are not obvious

Both cost real debugging time, and both fail while looking like they
worked.

### The node has its own image store

Docker Desktop's current Kubernetes runs the cluster inside a container
(`desktop-control-plane`) with its **own containerd store**. That store is
not the Docker engine's. A tag `docker images` lists plainly can still
fail with `ErrImagePull`, because the kubelet never looks there —
`imagePullPolicy: IfNotPresent` judges "present" against the node's store,
not yours.

```
docker images          ->  1.0.0, 1.1.0, dev, qs, latest
crictl images (node)   ->  1.1.0            (pulled from Docker Hub)
```

So a locally built image has to be imported. `run.bat k8s-build` does it,
and `k8s-up` does it for tags that do not exist yet:

```powershell
docker save <image>:<tag> -o t.tar
docker exec -i desktop-control-plane ctr -n k8s.io images import - < t.tar
```

It detects rather than assumes: older Docker Desktop releases used a node
that did share the engine's store, and if the node's name is not also a
running container the import is skipped.

### NodePorts are not published to Windows

The node container publishes exactly one port to the host — 6443, the API
server. kubectl works perfectly while `http://localhost:30080` refuses the
connection, with a healthy Service and healthy endpoints on the other side
of the boundary.

```
docker port desktop-control-plane      ->  6443/tcp -> 127.0.0.1:62526
curl from inside the cluster network   ->  30080=200 30081=200 30082=200
```

`type: LoadBalancer` is not the way out: this cluster has no provisioner,
so such a Service sits at `<pending>` indefinitely.

`run.bat k8s-up` therefore starts a small `socat` container, `rms-gateway`,
on the cluster's own Docker network, forwarding the three ports from
Windows to the node. Traffic arrives as real NodePort traffic and is
load-balanced across the replicas by kube-proxy with the Service's
affinity intact — which `kubectl port-forward` would not do, since it pins
to one pod and bypasses the Service. `run.bat k8s-down` removes it.

---

## Replicas

The previous version of this file said replicas must stay 1, because the
data plane is SQLite on a ReadWriteOnce volume. That was too strong.
**ReadWriteOnce means one NODE, not one pod** — several pods on the same
node may mount the same claim, which is exactly what production does:

```
rms-production  dashboard-58854d699c-5lfcj  Ready  desktop-control-plane
rms-production  dashboard-58854d699c-7cxpl  Ready  desktop-control-plane
rms-production  dashboard-58854d699c-92ctz  Ready  desktop-control-plane
```

What is still true is that scaling past **one node** needs Postgres, not a
bigger number. Three settings keep that honest:

**`podAffinity`, preferred.** Keeps replicas on one node so the
ReadWriteOnce promise holds. Deliberately not `required`: a required rule
selecting the pod's own label has nothing to match when the first pod of a
fresh namespace is scheduled, and risks wedging the very first rollout.

**`maxSurge: 0`.** The load-bearing setting, not `RollingUpdate` itself. A
surge pod is an *extra* pod, and the scheduler may place an extra pod on
another node — where the claim is already attached, so it would block
forever. Holding the surge at zero terminates a pod before creating its
replacement, so the count never exceeds `replicas` and no second node is
ever needed. With replicas > 1 that still leaves N-1 serving throughout,
which `Recreate` (a full outage) did not.

**`sessionAffinity: ClientIP`.** Required once replicas > 1. Streamlit is
not stateless: the browser loads the page over HTTP, then upgrades to a
websocket carrying the session, and the server keeps that session in the
memory of one pod. Round-robin sends the upgrade to a different pod than
served the page, which the user meets as a permanent "Connecting..." or a
view that resets on every click.

Affinity also covers a quieter problem. `config.py` memoises values in a
**per-process** dict, invalidated only by the process that wrote them and
with no TTL. A config edit applied on one pod leaves the others serving
the old value until they restart. Affinity means whoever made the edit
keeps talking to the pod that made it. The others converge on restart —
and `k8s-seed` restarts them for exactly this reason. If that ever needs
to be immediate, config belongs in Postgres.

One consequence to be honest about: the only writer is the pipeline, and
it is never part of this Deployment. `run.bat k8s-pipeline <env>` runs it
inside one pod, giving a single writer alongside the readers, which is
what WAL plus `busy_timeout=5000` (see `database/connection.py`) is for.

---

## Layout

```
k8s/
  base/                    one environment, defined once
    deployment.yaml        pod, probes, rollout strategy, pod affinity
    service.yaml           NodePort + ClientIP session affinity
    pvc.yaml               2Gi ReadWriteOnce claim
    kustomization.yaml
  overlays/
    production/            namespace, :1.1.0, 3 replicas, nodePort 30080
    dev/                   namespace, :dev,   1 replica,  nodePort 30081
    qs/                    namespace, :qs,    2 replicas, nodePort 30082
```

Each overlay is four small files. Everything else is inherited, so a
change to the pod definition reaches all three — which is the intent for
*shape*, and precisely why *code* is pinned per environment instead.

---

## The resource blocks are the point

`resources.requests` is the allocation decision this entire project exists
to compute. `service/recommender.py` produces, every cycle, the smallest
allocation that holds the SLA:

```
cpu_percent
  recommended : 99.74%  (~7.98 vCPUs)
  monthly cost: $235.79  (static $236.40)
```

In Kubernetes that is `resources.requests.cpu: "7980m"`. The three overlays
set those values by hand today. Wiring the recommender's output into them
is the step that would turn a recommendation into an action — and it is
deliberately not done yet, because the saving has not been re-measured
since the disk schema changed.

---

## What this deployment does not do

It runs the dashboard, not the collector. `psutil` reads `/proc`, which
belongs to the node, not the pod: a collector pod capped at `500m` still
reports all 16 cores. Verified, not assumed —

```
docker run --rm --cpus 0.5 --memory 512m … python -c "import psutil; …"
cpu_count 16
mem_total_gb 7.5
```

So per-pod monitoring would report identical node-level numbers from every
pod while looking like it worked. Collecting inside Kubernetes requires
the collector to read `/sys/fs/cgroup/cpu.stat` and `memory.current`
first. Until then, collect on the host with `run.bat collect` and treat
these namespaces as places to serve and inspect the results.

---

## Private registries

`imagePullPolicy: IfNotPresent` falls back to Docker Hub on any cluster
that is not this one. If the repository is private there, create a pull
secret in each namespace:

```powershell
kubectl create secret docker-registry regcred `
  --docker-username=ammaartech --docker-password=<token> `
  -n rms-production
```

and add `imagePullSecrets: [{name: regcred}]` to the pod spec.
