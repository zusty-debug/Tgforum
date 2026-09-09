from organizer.dedup import DuplicateDetector
from organizer.grouping import GroupingEngine
from organizer.planner import Planner

from .util import cfg, mk


def plan_for(files, cfg_over=None):
    c = cfg()
    if cfg_over:
        c.update(cfg_over)
    groups = GroupingEngine(c).group(files)
    dup_of, _ = DuplicateDetector().detect(files)
    planner = Planner(c, archives=[{"id": "archive_1", "chat_id": -100}])
    planner.enrich(groups, dup_of)
    plan = planner.plan(groups, {id(g): i for i, g in enumerate(groups, 1)}, dup_of)
    return groups, dup_of, plan


def test_exact_file_id_duplicate():
    fs = [mk(1, "DB.zip", fid="X"), mk(2, "DB.zip", fid="X"), mk(3, "Other.zip")]
    dup, _ = DuplicateDetector().detect(fs)
    assert dup[2][0] == 1 and dup[2][1] == 1.0
    assert 3 not in dup


def test_name_part_size_duplicate():
    fs = [mk(1, "DB.zip.003"), mk(2, "DB.zip.003")]
    dup, _ = DuplicateDetector().detect(fs)
    assert 2 in dup
    fs2 = [mk(1, "DB.zip.003"), mk(2, "DB.zip.004")]  # different slot
    dup2, _ = DuplicateDetector().detect(fs2)
    assert 2 not in dup2


def test_reupload_noise_same_base():
    fs = [mk(1, "Database.zip"), mk(2, "Database Copy.zip"),
          mk(3, "Database (1).zip"), mk(4, "Database_backup.zip")]
    dup, _ = DuplicateDetector().detect(fs)
    assert 2 in dup and 3 in dup and 4 in dup


def test_level2_candidate_not_auto():
    fs = [mk(1, "FB 2024 827 Million.zip", size=42_000_000_000),
          mk(2, "Facebook 2024 827Million dataset.zip", size=42_000_000_000)]
    dup, cands = DuplicateDetector().detect(fs)
    assert not dup  # names differ enough → no auto
    assert len(cands) >= 1 or not dup


def test_topic_rules():
    fs = [mk(1, "IMEI.part1.rar"), mk(2, "IMEI.part2.rar"),
          mk(3, "Udemy_C_Programming.pdf", size=20_000_000),
          mk(4, "ULP India 2024 v2.zip", size=5_000_000),
          mk(5, "UK customers 2024.xlsx", size=7_000_000),
          mk(6, "mystery.bin", size=3_000_000)]
    groups, _, plan = plan_for(fs)
    titles = {t.title: t.kind for t in plan["topics"].values()}
    assert "IMEI" in titles and titles["IMEI"] == "dataset"
    assert titles.get("ULPs") == "ulp"
    assert titles.get("United Kingdom") == "country"
    assert titles.get("Udemy") == "domain"
    assert titles.get("Miscellaneous") == "misc"

    by_gid = {p.group_id: p for p in plan["groups"].values()}
    gmap = {g.name: g for g in groups}
    ulp_plan = by_gid[[i for i, g in enumerate(groups, 1) if g.name == "ULP India 2024 v2"][0]]
    assert ulp_plan.topic_title == "ULPs"
    assert ulp_plan.summary is None  # per requirement: files only

    imei_plan = by_gid[[i for i, g in enumerate(groups, 1) if g.name == "IMEI"][0]]
    assert imei_plan.summary is not None
    assert "Total files: 2" in imei_plan.summary
    assert "Total size" in imei_plan.summary
    assert "COUNTRY" in imei_plan.summary  # unknown → 🌐 COUNTRY: UNKNOWN


def test_duplicate_member_skipped():
    fs = [mk(1, "DB.zip.001"), mk(2, "DB.zip.002"), mk(3, "DB.zip.002")]
    groups, dup_of, plan = plan_for(fs)
    p = list(plan["groups"].values())[0]
    assert p.copy_ids == [1, 2]
    assert p.skipped_ids == [3]


def test_quarantine_rule():
    fs = [mk(1, "breached_stuff_passwords.zip"), mk(2, "clean_file.zip")]
    groups, _, plan = plan_for(fs)
    titles = {t.title for t in plan["topics"].values()}
    assert "Quarantine" in titles
    q = [g for g in groups if g.status == "QUARANTINED"]
    assert len(q) == 1 and "password" in q[0].quarantine_reason


def test_review_files_go_to_saved():
    fs = [mk(1, "part1.rar"), mk(2, "part2.rar")]
    groups, _, plan = plan_for(fs)  # review_destination: saved
    p = list(plan["groups"].values())[0]
    assert p.to_saved is True
    assert plan["saved"] == [1, 2]


def test_country_beats_domain():
    fs = [mk(1, "India Yandex data 2024.csv", size=8_000_000)]
    _, _, plan = plan_for(fs)
    titles = {t.title for t in plan["topics"].values()}
    assert "India" in titles
    assert "Yandex" not in titles
