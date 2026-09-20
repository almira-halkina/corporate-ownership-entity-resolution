# Updating the live site

Render is watching your GitHub repo. The whole loop is: change a file, push,
wait. There is no separate deploy step and no CLI.

```powershell
cd C:\Users\almir\OneDrive\Documents\Claude\Projects\professional\jobs\corporate-ownership-entity-resolution
git add -A
git commit -m "what changed"
git push
```

Render picks up the push within a few seconds and rebuilds. Watch it at
[dashboard.render.com](https://dashboard.render.com) → your service → **Events**.
A build takes roughly 3–6 minutes, because it installs the package, runs the
whole resolution pipeline over the corpus, and bakes the resulting index into
the image.

When it says **Live**, open the site and **hard-refresh** — <kbd>Ctrl</kbd> +
<kbd>F5</kbd>. Without it your browser may serve the cached page and you will
think the deploy failed when it did not.

If Auto-Deploy was switched off when you created the service, turn it on under
Settings → Build & Deploy → Auto-Deploy, or hit **Manual Deploy → Deploy latest
commit** each time.

---

## Where to change what

| To change… | Edit | Notes |
|---|---|---|
| Any wording, the About tab, how-to-use, methodology | `src/ownership_er/serve/static/index.html` | Plain HTML near the top of the file, in the three `<section>` blocks |
| Chart colours, layout, fonts | same file, the `<style>` block | Colours are CSS variables at the very top, defined once per theme |
| A chart's data, or a new statistic | `src/ownership_er/serve/api.py` → the `overview()` function | Add it to the returned dict, then render it in `loadOverview()` in the HTML |
| A new API endpoint | `src/ownership_er/serve/api.py` | Add a test in `tests/test_serve.py` at the same time |
| What the index contains | `src/ownership_er/serve/index.py` | This is the offline build step; changing it changes what the API can answer |
| The corpus itself | `fixtures/` | The image build reruns the pipeline, so a new fixture set flows through automatically |

**The dashboard numbers are not hard-coded anywhere.** They are computed from
the index at request time, so if the corpus changes, every figure, chart and
caption count updates on the next deploy without anyone editing text.

---

## Before you push, if you changed code

```powershell
python -m pytest tests/test_serve.py -q
```

19 tests. Several of them exist specifically to catch a dashboard that lies —
that the headline count matches the table beneath it, that companies listed as
"clean filings, sanctioned upstream" really are not sanctioned themselves, and
that the precomputed sanctions closure still agrees with a live traversal.

To see it locally before the world does:

```powershell
python -m ownership_er.cli serve --port 8080
```

---

## If a deploy fails

Render → your service → **Logs**. The build runs these in order, and the
failure will name which one stopped:

```
pip install .
python -m ownership_er.cli normalize --fixtures
python -m ownership_er.cli block
python -m ownership_er.cli match --matcher rules
python -m ownership_er.cli cluster
python -m ownership_er.serve.index
```

A failure in the last step almost always means an earlier stage produced an
empty warehouse. A failure at `pip install` means a dependency changed in
`pyproject.toml`.

The previous version stays live while a build runs, and stays live if the build
fails — so a broken push never takes the site down.

---

## Two things worth doing once

**Put the URL on the repo.** GitHub → your repo → the gear icon beside *About*
→ paste the Render URL into **Website**. It is the highest-traffic place a link
can sit, and it takes ten seconds.

**Point a monitor at it.** [uptimerobot.com](https://uptimerobot.com), free: add
an HTTP monitor on `https://<your-site>/health` every 5 minutes. After a few
weeks you have a real uptime figure you can quote. It also keeps Render's free
tier from sleeping, which removes the cold-start delay for anyone clicking your
link — the reason to do this is as much the demo as the metric.
