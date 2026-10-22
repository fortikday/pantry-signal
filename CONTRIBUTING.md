# Contributing and running checks

The app runs with Python 3.11+ and no third-party packages. `requirements.txt`
documents that empty runtime dependency set. The Python tests use the exact
versions recorded in `requirements-dev.txt`, including pytest's dependencies.

Run commands from this project directory. Create an isolated environment first:

```sh
python -m venv .venv
```

On Windows, use that environment's Python directly; activation is optional:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
```

On macOS or Linux:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

The Python tests create temporary databases and local HTTP listeners. They do
not access your running demonstration or its `local/` data folder.

## Front-end checks

Install Node.js 24 to run the JavaScript tests. Node is needed for development
checks only, not to launch the app. No npm packages are required.

```sh
node --test tests/frontend/*.test.cjs
```

These checks execute the actual inline JavaScript from `index.html` with a
simulated DOM, network responses, and timers. They cover behavior such as
outages, freshness warnings, retry references, and recipe drafts. They are not
browser rendering, layout, accessibility, or full end-to-end tests. The earlier
browser walkthrough in `results/verification.json` is recorded separately as
manual evidence; it is not counted as automated coverage.

## Product website

The public product website lives in `docs/`. It uses plain HTML, CSS, and
JavaScript, with no build step, external assets, or runtime packages. GitHub
Pages publishes this directory from `main`.

Preview just the website, independently of the restaurant services:

```sh
python -m http.server 8780 --bind 127.0.0.1 --directory docs
```

Open `http://127.0.0.1:8780/`. The interactive example uses fictional quantities
in browser memory. It never contacts the restaurant services and does not save
orders or stock between visits.

Run the website's behavior checks together with the app's front-end checks:

```sh
node --test tests/frontend/*.test.cjs tests/website/*.test.cjs
```

The website checks also use a simulated DOM. After visual changes, check the
website in a browser at desktop and narrow mobile widths. Verify the sample's
order, preparation, ready, pause/resume, and reset controls. The layout and
browser check results are recorded separately in `results/website-verification.json`.

## Manual browser check

To repeat the main workflow without using an existing log, run:

```sh
python app.py --data-dir local-review --orders-port 8870 --inventory-port 8871
```

1. Confirm inventory is empty, then explicitly load the fictional deliveries.
2. Send a Caesar salad with chicken and shrimp to Table 7. Chicken should show
   5,000 g on hand and 400 g reserved after inventory catches up.
3. Start preparing. Chicken should become 4,600 g on hand and 0 g reserved.
4. Mark ready. Chicken should remain 4,600 g.
5. Pause ingredient updates. Confirm the stock warning is visible beside the
   quantities and the quantities are marked as the last estimate.
6. Send another identical order and start preparing it while updates are paused.
7. Resume. After catch-up, chicken should be 4,200 g; the stale warning should
   clear and the last-sync time should advance.
8. Edit a recipe quantity without saving. Wait through several refreshes and
   confirm the draft stays intact. Reload saved quantities to discard the draft.

`local-review/` is ignored by Git. Both services must be stopped before copying
their databases. Use another unused folder and port pair for a fresh repeat.

## Scope of changes

Keep order/kitchen writes independent of inventory availability. Commit events
with their source changes and apply inventory events with their cursor in one
transaction. Preserve request references on uncertain retries.

Before an operational pilot, agree with a restaurant on approved recipes,
portion measurements, preparation yields, and how staff correct mistakes. A
future correction for a mistakenly ready ticket should be an audited kitchen
transition back to preparing; it should not reverse ingredient usage already
recorded at the start of preparation. Prepared subrecipes need batch and yield
accounting rather than only a new field on a menu item. Authentication,
restaurant isolation, and deployment controls are prerequisites for a hosted
version. These capabilities are not implemented in this local prototype.
