# Telegram Archive Organizer

Turns a ~90,000-file Telegram channel into **multiple organized forum
archives** (with a searchable `index.html`) — fully automatic,
resumable, rate-limit-aware, and safe to rerun.

Built for: one **private source channel** → **2–3 private forum
supergroups** with identical organization, run on a small VPS or PaaS
(Render / Railway), 512MB–1GB RAM friendly (streaming + SQLite, **no
file downloads**).

## How it works

```
source channel ──▶ scan (metadata only, resumable)
                     │  SQLite: message ids, file ids, names, sizes, dates
                     ▼
                   classify (offline engine)
                     • logical grouping — weighted multi-signal score
                       (name similarity · part numbering · archive family ·
                        size · timestamps · neighborhood · media groups)
                       — NOT a regex list; every decision is explainable
                     • two-level duplicate detection (file_id / name+part+size)
                     • evidence-based country detection (aliases, TLDs, cities,
                       scripts) with confidence
                     • category classification + ULP keyword rule
                     • quarantine safety net (breach/credential/PII names)
                     ▼
                   dry-run  ← full report BEFORE anything touches Telegram
                     ▼
                   sync (parallel per forum, silent copies — no
                     "Forwarded from" label, no re-download)
                     • one topic per multi-part dataset (named from the
                       FIRST filename) — up to 6,000, then "Extra Datasets N"
                     • after each dataset: summary message
                       (country · files · total size) — POSTED AND PINNED
                     • single files → country topic → domain/service topic
                       → Miscellaneous
                     • all ULP files → one "ULPs" topic (files only)
                     • unresolvable files → your Saved Messages for approval
                     ▼
                   rebuild-index → index.html + data.json (searchable,
                   offline, no backend)
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill TELEGRAM_API_ID/HASH, SOURCE_CHAT_ID,
                              # ARCHIVE_1/2/3_CHAT_ID
python main.py auth           # one-time login → saves session to .env
python main.py discover-chats # (find your chat IDs if unsure)

python main.py scan           # metadata ingest (resumable, ~20–40 min)
python main.py classify       # build logical groups
python main.py dry-run        # THE full plan — nothing is posted yet
python main.py review         # inspect medium-confidence / unresolvable items
python main.py approve 12 --action accept      # (| ignore | rename --name X
                                               #  | quarantine | mark-dup)
python main.py sync-all       # build all forums in parallel (resume-safe)
python main.py rebuild-index  # out/index.html + out/data.json
```

Useful variants:

```bash
python main.py sync --archive archive_1 --limit 10   # pilot: first 10 groups
python main.py resume                                 # continue after a crash
python main.py status                                 # progress at a glance
```

### Expected workflow (from the spec)

1. Configure source + destination chats in `.env`
2. `python main.py auth` (authorized user client with manage-topics rights)
3. `python main.py scan`
4. `python main.py classify`
5. Review medium-confidence + quarantine items (`review` / dry-run report)
6. `python main.py dry-run` — verify proposed topics & grouping
7. `python main.py sync --archive archive_1 --limit 10` (pilot)
8. `python main.py sync-all` (the long run — days, fully resumable)
9. `python main.py rebuild-index`, serve `out/` with nginx/caddy

## Topic rules (as agreed)

| Material | Destination |
|---|---|
| Multi-part dataset (any split convention) | **own topic** named from the first filename; later same-name files reuse it |
| …after all parts are copied | summary message `🌐/flag COUNTRY · 📁 name · 📦 parts · 📄 files · 💾 total size` — **pinned** |
| Dataset-topic cap | 6,000 own topics (smallest overflow into `Extra Datasets N`, 50 datasets each) |
| Single file | **country** topic (if confidence ≥ 0.75) → domain/service topic → `Miscellaneous` |
| Any file with "ULP" in the name | one `ULPs` topic — files only, no summaries |
| Can't be confidently resolved | your **Saved Messages** + CLI approval (`python main.py approve …`) |
| Quarantine-pattern names | `Quarantine` topic (safety net; never auto-redistributed) |
| Duplicate files | copied once per forum; later copies skipped; source never touched |

## Deployment (Render / Railway)

No VPS needed. Both configs are included.

**Render** — `render.yaml` is already set up: a *worker* service with a
persistent disk at `/data` (point `DB_PATH` at `/data/archive.db`).
Create the service from the repo, fill the env values in the dashboard,
deploy. Each deploy reruns `start.sh` — scan is incremental, sync resumes
from checkpoints, so a restart mid-run costs nothing.

**Railway** — create a service from the repo (`railway.json` is read
automatically), attach a **Volume** at `/data`, set the same env vars,
deploy.

Both platforms: the long sync runs days; just let the service stay up.
If it restarts for any reason, `start.sh` picks up exactly where it
stopped (job-level checkpoints in SQLite).

## Configuration

Everything lives in `config.yaml` (copy `config.example.yaml`):

- `classification.auto_group_threshold` (0.90) / `review_threshold` (0.65)
- `organization.max_topics_per_forum` (6000) + `overflow_topic_capacity` (50)
- `organization.review_destination`: `saved` (default) | `topic` | `hold`
- `organization.known_services`: extend with your domain/brand names
- `quarantine.patterns`: the safety-net keywords
- `processing.sync_delay_seconds` (1.2) / `max_parallel_archives` (3)
- `index.single_file_html`: true → one self-contained index.html

Chat IDs + credentials are **env-only** (`.env`, gitignored). Nothing
secret is ever logged.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers the spec's exact examples: `.partNN.rar`, `.zip.007`,
`.z01`+final `.zip`, `.7z.019/.020`, missing parts, mid-range sequences,
mixed conventions (Yandex EDA), contradictory names, duplicates,
country/category/ULP/quarantine rules, the full DB→report→index pipeline.

## Operating notes

- **Speed**: 90k files × 3 forums ≈ 270k sends. At a flood-safe ~1 msg/s
  per forum (3 in parallel) expect **1–4 days** for the full sync.
  `sync --limit N` pilots are minutes.
- **Disk**: only SQLite state (~100–300 MB). No files are ever downloaded.
- **Reruns**: rerun anything any time — topic reuse is by exact title,
  copies are job-checkpointed, duplicates are skipped. No "Database 2",
  no double posts.
- **Legal note**: the pipeline is metadata-only and includes a quarantine
  safety net for names suggesting unauthorized material. Keep it that way
  (no content inspection) to stay on the right side of the line.
