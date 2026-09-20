# Deploying the service

Everything here is written and tested. What is left needs your accounts, so it
is yours to run. Budget 20–30 minutes end to end.

Nothing below asks you to write code.

---

## 0. Check it locally first (5 min, no accounts)

```bash
cd corporate-ownership-entity-resolution
pip install -e ".[serve,dev]"

python -m ownership_er.cli normalize --fixtures
python -m ownership_er.cli block
python -m ownership_er.cli match --matcher rules
python -m ownership_er.cli cluster
python -m ownership_er.cli build-index

python -m ownership_er.cli serve --port 8080
```

Open `http://127.0.0.1:8080`. Search `Ravensworth`, click the result: no
sanctions topics of its own, and a sanctioned PEP three hops up.

Tests and the load test:

```bash
python -m pytest tests/test_serve.py -q          # 14 tests
python bench/sweep.py                            # rebuilds the README table
```

---

## 1. Deploy to Fly.io (15 min)

Fly is the recommendation because the app is a single container with no
database, and Fly scales it to zero between visits — with a ~0.7 s cold start,
a reviewer never notices, and an idle demo costs approximately nothing.

**Install and sign in.**

```bash
# macOS/Linux:  curl -L https://fly.io/install.sh | sh
# Windows PowerShell:  iwr https://fly.io/install.ps1 -useb | iex
fly auth signup      # or: fly auth login
```

A card is required even on the free allowance. This app is one
`shared-cpu-2x` / 512 MB machine that sleeps when idle.

**Launch.** From the repo root, where `fly.toml` already is:

```bash
fly launch --no-deploy --copy-config --name ownership-graph
```

Pick a name that is free — if `ownership-graph` is taken, use something like
`ownership-graph-halkina`, and let it update `fly.toml`. Say **no** to Postgres,
Redis and any other add-on: the index ships inside the image.

**Deploy.**

```bash
fly deploy
```

The first build takes a few minutes: it installs the package, runs the pipeline
over the fixtures, builds the serving index and bakes it into the image.

**Check it.**

```bash
fly open                      # the UI
fly status
curl https://<your-app>.fly.dev/health
curl "https://<your-app>.fly.dev/api/sanctions/exposed?min_hops=2&limit=3"
```

**If the deploy fails**, `fly logs` is the first place to look. The two most
likely causes are a name collision (rename in `fly.toml`) and a build timeout
on a slow connection (`fly deploy --remote-only` builds on Fly's machines).

### Alternative: Render

Render's free web services also work and need no card, but they sleep after 15
minutes and cold-start in tens of seconds rather than under a second, which is
a worse demo. If you prefer it: New → Web Service → connect the repo → Docker →
leave everything default. The `Dockerfile` needs no changes.

---

## 2. Point a monitor at it (5 min)

This is what turns "deployed" into a number you can put on a resume.

1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free).
2. Add New Monitor → HTTP(s) → `https://<your-app>.fly.dev/health`, interval 5
   minutes.
3. Leave it. After a few weeks you have a real uptime figure, and after 90 days
   you can quote it honestly.

Note that with `auto_stop_machines` on, the monitor will wake the app every 5
minutes, so it will rarely sleep. That is the trade: a live uptime number, or a
genuinely idle app. Either is defensible — if you would rather it sleep, monitor
every 30 minutes instead.

---

## 3. Push it (2 min)

```bash
git add -A
git commit -m "Add online serving layer: index build, API, UI, load tests, deploy"
git push
```

The `service` workflow runs on push: it builds the index, runs the 14 serving
tests, starts the app, load-tests it, and **fails the build on a single error
under load**. A second job builds the Docker image and curls the running
container.

---

## 4. Put the link where it counts

- **GitHub repo → About → Website**: the Fly URL. This is the highest-traffic
  place a link can sit.
- **README**: first line under the title.
- **Resume**: the projects line for this repo becomes a live link. Say the word
  and I will rebuild the FDE resume with it.

---

## What to say about it in an interview

The question you will get is "what was hard", and the honest answer is the
concurrency bug, because it is the one that shows judgement rather than effort:

> The functional tests all passed. The first load test at sixteen concurrent
> clients returned a nineteen percent error rate, and the exceptions looked like
> data corruption — `'float' object is not iterable`, tuple index out of range.
> One shared DuckDB connection across FastAPI's thread pool: two threads calling
> execute on the same handle interleave and read each other's result sets. One
> cursor per worker thread fixed it — zero errors, and throughput went from 155
> to 225 requests per second. No functional test would ever have caught it,
> because it cannot happen below two concurrent requests. That is why the load
> test runs in CI now.

The second thing worth having ready is why there is no database: the serving
index is build output, not state, so the deploy is one immutable container and
a rollback is a tag change. Neo4j is still the right answer at national scale,
and the loader still targets it — but requiring a graph server to answer
questions about 1,786 entities would be infrastructure theatre.

---

## Appendix: running this on Windows / PowerShell

PowerShell is fine for all of it. Five things differ from the commands above,
and the first one will bite immediately.

**1. `curl` is not curl.** In PowerShell, `curl` is an alias for
`Invoke-WebRequest`, which does not understand `-sf` and returns an object
rather than text. Use `curl.exe` (real curl ships with Windows 10+):

```powershell
curl.exe -s http://127.0.0.1:8080/health
curl.exe -s "http://127.0.0.1:8080/api/sanctions/exposed?min_hops=2&limit=3"
```

Or use the native equivalent, which pretty-prints JSON for free:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health | ConvertTo-Json -Depth 5
```

**2. `&&` needs PowerShell 7.** Windows PowerShell 5.1 — the blue one, still
the default on many machines — does not support `&&`. Check with
`$PSVersionTable.PSVersion`. Either install PowerShell 7 (`winget install
Microsoft.PowerShell`) or just run the pipeline stages on separate lines:

```powershell
python -m ownership_er.cli normalize --fixtures
python -m ownership_er.cli block
python -m ownership_er.cli match --matcher rules
python -m ownership_er.cli cluster
python -m ownership_er.cli build-index
```

**3. Environment variables** use `$env:` rather than `export`:

```powershell
$env:OER_SERVING_INDEX = "$PWD\data\warehouse\serving.duckdb"
```

**4. Quote the extras.** `pip install -e .[serve,dev]` unquoted is parsed as an
array by PowerShell. Keep the quotes:

```powershell
pip install -e ".[serve,dev]"
```

**5. Line continuation is a backtick**, not a backslash — but nothing here
needs one if you keep each command on its own line.

### One thing specific to where this repo lives

The repo sits under `OneDrive`. The pipeline writes `data/warehouse/*.duckdb`,
which is a multi-megabyte binary rewritten on every run, and the API holds it
open while serving. OneDrive will try to sync it mid-write, which at best
wastes bandwidth and at worst locks the file and fails a run.

`data/` should already be in `.gitignore`, but that does not stop OneDrive.
Exclude it from sync once:

1. Right-click the `data` folder → **OneDrive** → **Always keep on this device**
   off, or
2. better: OneDrive settings → Sync and back up → Advanced → **Exclude files** —
   or simplest of all, run the pipeline from a clone outside OneDrive, e.g.
   `C:\dev\corporate-ownership-entity-resolution`.

If a run fails with a permission or sharing-violation error on
`ownership.duckdb`, this is why.

### Docker

`docker build` and `docker compose up service` need **Docker Desktop running**
(it requires WSL2, which Windows will offer to install). Once it is up, both
commands are identical to the Linux ones. If you would rather not install
Docker at all, you do not have to: `fly deploy` builds the image on Fly's
machines with `--remote-only`.

```powershell
fly deploy --remote-only
```

### If PowerShell fights you

Two escape hatches, both already on most dev machines:

- **Git Bash** (ships with Git for Windows) runs every command in this document
  exactly as written, `curl` and `&&` included.
- **WSL2 Ubuntu** — if you install Docker Desktop you will have it anyway.
  `cd /mnt/c/Users/almir/...` reaches the same files, though running the
  pipeline against the Windows filesystem through `/mnt/c` is noticeably slower
  than cloning inside WSL.
