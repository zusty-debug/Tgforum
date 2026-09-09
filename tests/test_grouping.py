from organizer.grouping import GroupingEngine

from .util import cfg, mk


def groups_of(files):
    return GroupingEngine(cfg()).group(files)


def multipart(groups):
    return [g for g in groups if g.is_multipart]


def test_imei_part_n_rar_auto():
    fs = [mk(i, f"IMEI.part{i}.rar") for i in range(1, 5)]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 4
    assert g.status == "OK" and g.confidence >= 0.90
    assert g.part_numbers == [1, 2, 3, 4] and g.missing_parts == []
    assert g.name == "IMEI"


def test_facebook_z_series():
    fs = [mk(i, f"Facebook_2024_827Million.z{i:02d}") for i in range(1, 12)]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 11
    assert g.name == "Facebook 2024 827Million"
    assert g.status == "OK"
    assert g.missing_parts == []


def test_z_parts_plus_final_zip():
    fs = [mk(i, n) for i, n in enumerate(
        ["Database.z01", "Database.z02", "Database.z03", "Database.zip"], 1)]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 4
    assert g.status == "OK"
    assert g.part_numbers == [1, 2, 3]


def test_missing_part_recorded():
    fs = [mk(i, n) for i, n in enumerate(
        ["Dataset.001", "Dataset.002", "Dataset.004", "Dataset.005"], 1)]
    g = multipart(groups_of(fs))[0]
    assert g.part_numbers == [1, 2, 4, 5]
    assert g.missing_parts == [3]
    assert g.status == "OK"


def test_mid_range_sequence():
    fs = [mk(i, f"A.zip.{n:03d}") for i, n in enumerate((4, 5, 6, 7), 1)]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 4
    assert g.partial_start is True
    assert g.status in ("OK", "REVIEW")  # explainable either way


def test_mixed_conventions_go_to_review():
    """Spec example 2: YAeda.zip.004-007 + Yandex-EDA-Part-01-SQL.zip are
    investigated as ONE logical group (medium confidence → REVIEW), while
    genuinely unrelated files stay separate."""
    fs = [mk(i, n) for i, n in enumerate(
        ["YAeda.zip.004", "YAeda.zip.005", "YAeda.zip.006", "YAeda.zip.007",
         "Yandex-EDA-Part-01-SQL.zip"], 1)]
    groups = groups_of(fs)
    g = multipart(groups)[0]
    assert len(g.members) == 5
    assert g.status == "REVIEW"
    assert "weakest link" in " ".join(g.reasons)


def test_unrelated_csv_not_absorbed():
    fs = [mk(i, n) for i, n in enumerate(
        ["YAeda.zip.004", "YAeda.zip.005", "YAeda.zip.006", "YAeda.zip.007",
         "Yandex-EDA-Part-01-SQL.zip"], 1)]
    fs.append(mk(6, "yandex_eda.csv", size=12_345_678, date=1_700_003_600))
    groups = groups_of(fs)
    g = multipart(groups)[0]
    assert 6 not in g.member_ids


def test_contradictory_names_never_merge():
    fs = [mk(1, "AlphaCompany Financials 2024.rar"),
          mk(2, "ZuluGroup Payroll 2019.rar")]
    assert multipart(groups_of(fs)) == []


def test_far_apart_same_name_stay_separate():
    fs = [mk(1, "data.zip", date=1_600_000_000),
          mk(2, "data.zip", date=1_700_000_000, size=999)]
    assert multipart(groups_of(fs)) == []


def test_rar_parts_plus_zip_final_repack():
    """part01-03.rar + Database.zip (different container) → investigated,
    at least a REVIEW group — not silently split."""
    fs = [mk(i, n) for i, n in enumerate(
        ["Database.part01.rar", "Database.part02.rar", "Database.part03.rar",
         "Database.zip"], 1)]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 4
    assert g.status in ("OK", "REVIEW")


def test_same_part_slot_is_duplicate_not_merge_conflict():
    fs = [mk(1, "DB.zip.001"), mk(2, "DB.zip.002"), mk(3, "DB.zip.002")]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 3
    assert g.status == "OK"


def test_bare_part_filename_goes_to_review():
    fs = [mk(1, "part1.rar"), mk(2, "part2.rar")]
    g = multipart(groups_of(fs))[0]
    assert len(g.members) == 2
    assert g.status == "REVIEW"
    assert "admin" in (g.review_reason or "")
