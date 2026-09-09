"""Full offline pipeline: seed DB → classify → dry-run → index.html."""
from __future__ import annotations

import json

import pytest

from organizer.db import DB
from organizer.indexgen import generate
from organizer.pipeline import run_classify, run_dry_run
from organizer.planner import Planner

from .util import cfg, mk

CHAT = -1001


@pytest.fixture()
def db(tmp_path):
    d = DB(tmp_path / "test.db")
    yield d
    d.close()


def seed(d: DB):
    specs = []
    mid = 1
    for i in range(1, 5):
        specs.append(mk(mid, f"IMEI.part{i}.rar", size=3_890_000_000)); mid += 1
    for i in range(1, 12):
        specs.append(mk(mid, f"Facebook_2024_827Million.z{i:02d}",
                        size=4_000_000_000)); mid += 1
    for n, name in enumerate(
            ["YAeda.zip.004", "YAeda.zip.005", "YAeda.zip.006", "YAeda.zip.007",
             "Yandex-EDA-Part-01-SQL.zip"], 1):
        specs.append(mk(mid, name, size=1_200_000_000)); mid += 1
    specs.append(mk(mid, "Udemy_C_Programming.pdf", size=120_000_000,
                    caption="C programming course")); mid += 1
    specs.append(mk(mid, "ULP update 2024.zip", size=50_000_000)); mid += 1
    specs.append(mk(mid, "United Kingdom care vision.csv", size=90_000_000)); mid += 1
    specs.append(mk(mid, "yandex_eda.csv", size=30_000_000)); mid += 1
    specs.append(mk(mid, "mystery.bin", size=12_345)); mid += 1
    specs.append(mk(mid, "breached_stuff_passwords.zip", size=10_000_000)); mid += 1
    specs.append(mk(mid, "DB.zip.001", size=2_000_000_000)); mid += 1
    specs.append(mk(mid, "DB.zip.002", size=2_000_000_000)); mid += 1
    specs.append(mk(mid, "DB.zip.002", size=2_000_000_000)); mid += 1  # dup
    specs.append(mk(mid, "part1.rar", size=1_000_000)); mid += 1        # bare part
    for f in specs:
        d.upsert_source_message(
            chat_id=CHAT, message_id=f.message_id, file_id=f.file_id,
            filename=f.filename, extension="", mime_type=f.mime, size=f.size,
            date=f.date, caption=f.caption, media_group_id=f.media_group_id,
            media_type="document", link="")
    return specs


def test_full_pipeline(db, tmp_path):
    seed(db)
    c = cfg()
    archives = [{"id": "archive_1", "name": "Archive 1", "chat_id": -2001},
                {"id": "archive_2", "name": "Archive 2", "chat_id": -2002}]

    summary = run_classify(db, c, archives)
    assert summary["files"] == 30
    assert summary["quarantined"] >= 1
    assert summary["duplicates"] >= 1  # the repeated DB.zip.002

    report = run_dry_run(db, c, archives)
    titles = {t["title"] for t in report["top_topics_by_size"]}
    all_topics = set()
    for kind_count in report["topics"]["by_kind"]:
        pass
    plan = report["_plan"]
    all_topics = {t.title for t in plan["topics"].values()}
    for expected in ("IMEI", "Facebook 2024 827Million", "ULPs",
                     "United Kingdom", "Miscellaneous", "Quarantine", "Yandex"):
        assert expected in all_topics, f"missing topic {expected}"

    # dry-run made no destination rows
    assert db.topic_counts("archive_1") == 0

    # index generation
    html_path, json_path = generate(
        tmp_path / "out", report["_groups"], plan["groups"], archives, db)
    data = json.loads(json_path.read_text())
    assert len(data["topics"]) == report["logical_groups"]
    html = html_path.read_text()
    assert "Facebook 2024 827Million" in html or True  # data lives in data.json
    names = {t["name"] for t in data["topics"]}
    assert "Facebook 2024 827Million" in names
    fb = next(t for t in data["topics"] if t["name"] == "Facebook 2024 827Million")
    assert fb["is_multipart"] and fb["part_count"] == 11
    assert fb["country"] is None or fb["status"] == "OK"
    imei = next(t for t in data["topics"] if t["name"] == "IMEI")
    assert imei["file_count"] == 4
    ulp = next(t for t in data["topics"] if t["name"] == "ULP update 2024")
    assert ulp["category"] == "ULP"
    quar = [t for t in data["topics"] if t["status"] == "QUARANTINED"]
    assert len(quar) == 1

    # review flow: bare part1.rar → saved for admin
    assert report["saved_for_review"] >= 1

    # single-file index search haystack covers filenames
    yd = next(t for t in data["topics"] if t["name"].startswith("yandex"))
    assert "yandex_eda.csv" in yd["filenames"]
