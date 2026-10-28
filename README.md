# ytpanel

Django control panel for **yt-dlp** (django + celery + redis + postgresql): download queue with a
*max simultaneous downloads* scheduler, **auto retry (N times or infinite)** with exponential backoff,
**purge**, live dashboard with speed graph, per-user isolation, dark mode, all configuration in PostgreSQL.
Same concept as [wgetpanel](https://github.com/cumulus13/wgetpanel), different backend.

```
browser ─HTTP/JSON─► web (Django+gunicorn) ─► PostgreSQL (queue, settings, history)
                       │ live speed                       ▲
                       ▼                                  │
                     Redis ◄─ beat ─► ctl worker (tick 2s, reaper, auto-purge)
                                        │ celery task per download
                                        ▼
                       downloads worker ── spawns ──► core/runner.py (one process group per download)
                                                       └─ yt-dlp Python API, progress as JSON lines
```

```mermaid
graph LR
    classDef plain fill:#fff,stroke:#333,stroke-width:2px;
    classDef icon fill:#f9f9f9,stroke:#333,stroke-width:1px,rx:5,ry:5;
    classDef label fill:none,stroke:none,font-weight:bold;

    %% Components
    B_Icon[<img src='https://via.placeholder.com/50' width='50'/>] -->|HTTP/JSON| WA_Icon;
    WA_Icon[<img src='https://via.placeholder.com/50' width='50'/>] -->|Live Data| LS_Icon;
    WA_Icon --> PG_Icon;
    LS_Icon[<img src='https://via.placeholder.com/50' width='50'/>] --> LS_Text;
    Redis_Icon[<img src='https://via.placeholder.com/50' width='50'/>] <-->|BEAT| CW_Icon;
    PG_Icon[<img src='https://via.placeholder.com/50' width='50'/>] --> PG_Text;
    CW_Icon[<img src='https://via.placeholder.com/50' width='50'/>] --> CW_Text;
    CW_Text -->|CELERY TASK<br/>PER DOWNLOAD| DW_Icon;
    DW_Icon[<img src='https://via.placeholder.com/50' width='50'/>] -->|SPAWNS| CR_Text;
    CR_Text[CORE/RUNNER.PY<br/>one process group per download] -->|yt-dlp Python API,<br/>progress as JSON lines| CR_Details;

    %% Component Groups
    subgraph Browser Group
        B_Icon[BROWSER]
    end
    class B_Icon icon;

    subgraph WebApp Group
        WA_Icon[Web App<br/>DJANGO +<br/>GUNICORN]
    end
    class WA_Icon icon;

    subgraph LiveSpeed Group
        LS_Icon[Live Speed]
        LS_Text[queue, settings, history]
    end
    class LS_Icon icon;
    class LS_Text label;

    subgraph Redis Group
        Redis_Icon[Redis]
    end
    class Redis_Icon icon;

    subgraph Postgres Group
        PG_Icon[POSTGRESQL]
        PG_Text[queue, settings, history]
    end
    class PG_Icon icon;
    class PG_Text label;

    subgraph CTLWorker Group
        CW_Icon[CTL<br/>WORKER]
        CW_Text[tick 2s,<br/>reaper, auto-purge]
    end
    class CW_Icon icon;
    class CW_Text label;

    subgraph DownloadsWorker Group
        DW_Icon[DOWNLOADS<br/>WORKER]
    end
    class DW_Icon icon;

    subgraph CoreRunner Group
        CR_Text
        CR_Details
    end
    class CR_Text label;
    class CR_Details label;

    %% Styling for labels and text
    class Browser Group,WebApp Group,LiveSpeed Group,Redis Group,Postgres Group,CTLWorker Group,DownloadsWorker Group,CoreRunner Group fill:none,stroke:none;
```

[![Architecture](https://files.catbox.moe/muvrc6.jpg)](https://files.catbox.moe/muvrc6.jpg)

## Quick start
```
cp .env.example .env     # edit secrets
docker compose up -d --build        # http://localhost:8000
```
Without Docker (needs ffmpeg, PostgreSQL, Redis): `pip install -r requirements.txt`, export the variables of
`.env.example` plus `DATABASE_URL`, `REDIS_URL`, then
```
python manage.py migrate && python manage.py bootstrap && python manage.py collectstatic --noinput
gunicorn ytpanel.wsgi -b 0.0.0.0:8000
celery -A ytpanel worker -Q downloads -n dl@%h -c 8 -l INFO
celery -A ytpanel worker -Q celery -n ctl@%h -c 2 -l INFO
celery -A ytpanel beat -l INFO
```
Without `DATABASE_URL`/`REDIS_URL` it falls back to SQLite + in-process cache (dev only, one process).

## Auto retry
Per download (and default in Settings): a number `N` (1 try + N retries), `0` (no retry) or **`-1` / "∞ always"**.
Delay = `base * 2^(attempt-1)`, capped, +≤10% jitter (Settings: base 10 s, cap 900 s). You can change the number
of retries of a running/queued download, or press *retry now*. Each retry **resumes** from the `.part` file
(HTTP Range) instead of restarting.

**Permanent errors are not retried, even with "always"** (private/removed/unavailable video, unsupported URL,
HTTP 404/410, requested format unavailable, playlist URL): looping forever on them only wastes resources.
Detection is by message text (`core/services/retry.py::PERMANENT_MARKERS`); enable *Retry permanent errors*
in Settings to retry everything. A worker crash is requeued by the reaper and does **not** use up a retry.

## Purge
Dashboard → *Purge* (preview first), `POST /api/purge/`, or `python manage.py purge [--status failed]
[--older-than-days 30] [--delete-files] [--dry-run]`. Removes completed/failed/cancelled entries and always their
leftover `.part` / `.ytdl` / `.part-Frag*` / `.fNNN.ext` files; downloaded files are only deleted with
*delete files*. Never touches running/queued/paused items; every deletion is confined to the allowed
directories (symlinks refused). Settings → *retention days* auto-purges periodically.

## Backends
`yt-dlp` (default, via its Python API) or `youtube-dl` (same API; **not tested here**, and unmaintained —
install it yourself). Pick in Settings. For YouTube, yt-dlp needs a JS runtime (`deno` ships via the
`yt-dlp[deno]` extra; `node`/`bun`/`quickjs` selectable) and often a cookies file (Settings). YouTube
changes constantly: `pip install -U yt-dlp` / rebuild the image regularly.

## Security
Login required; settings need `is_staff`; users only see/act on their own downloads (staff see all). CSRF on all
POSTs, HttpOnly cookies. Runner gets a minimal environment (no `DJANGO_SECRET_KEY`/`DATABASE_URL`). Output
dirs and purge/delete confined to allowed directories. **SSRF guard** (on): URLs resolving to private/loopback/
link-local addresses are rejected — extractors follow redirects themselves and DNS can change after the check,
so for hostile multi-tenant use also block internal ranges with an egress firewall.

## Limitations
- Single videos only; playlist URLs are rejected (add the video URLs).
- POSIX is the supported platform (process groups); Windows path uses plain terminate, untested.
- `max simultaneous downloads` ≤ `YTP_WORKER_CONCURRENCY`, otherwise claimed downloads wait for a free worker.
- Crash recovery takes ~60–70 s (heartbeat grace + reaper interval).
- No stall watchdog: a hung (not failing) download is only bounded by the socket timeout.

## Tests
```
export DATABASE_URL=postgres://user:pass@localhost/db   # role needs CREATEDB
export TEST_REDIS_URL=redis://localhost:6379/15         # DB is flushed!
python manage.py test        # 67 tests: real yt-dlp against a local Range-capable server
python -m core.tests.fixture_server 8765 &              # + deploy/smoke_test.py against a running stack
```
Covered: completion + checksum, 404/503, finite/infinite/zero retries, backoff, permanent errors, mid-stream drop
→ Range resume, pause/resume/cancel, rate limit, scheduler limit incl. 8 concurrent ticks, reaper + orphan kill,
purge (all options), API permissions/CSRF/isolation/path traversal/SSRF.
**Not tested:** real YouTube (no internet in the dev sandbox), the Docker files, ffmpeg merge/audio presets.

## License

[LICENSE](LICENSE)

---

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)