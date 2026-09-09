from organizer.partparser import parse_filename as p


def test_part01_rar():
    i = p("Database.part01.rar")
    assert i.base == "Database" and i.family == "part" and i.part_number == 1
    assert i.container == "rar" and not i.is_final_container


def test_part1_rar():
    i = p("Database.part1.rar")
    assert i.part_number == 1 and i.base == "Database"


def test_part_with_hyphen():
    i = p("Movie.part-01.rar")
    assert i.family == "part" and i.part_number == 1 and i.base == "Movie"


def test_zip_nnn():
    i = p("YAeda.zip.004")
    assert i.base == "YAeda" and i.family == "nn"
    assert i.part_number == 4 and i.container == "zip"


def test_zip_007():
    i = p("Database.zip.007")
    assert i.base == "Database" and i.part_number == 7 and i.container == "zip"


def test_z01():
    i = p("Facebook_2024_827Million.z01")
    assert i.family == "z" and i.part_number == 1
    assert i.base == "Facebook_2024_827Million"


def test_z99():
    assert p("Archive.z99").part_number == 99


def test_7z_nnn():
    i = p("Database.7z.019")
    assert i.family == "nn" and i.part_number == 19 and i.container == "7z"
    i = p("Database.7z.020")
    assert i.part_number == 20


def test_final_zip():
    i = p("Database.zip")
    assert i.part_number is None and i.is_final_container and i.container == "zip"


def test_generic_nnn():
    i = p("Dataset.001")
    assert i.family == "nn" and i.part_number == 1 and i.base == "Dataset"


def test_nn2():
    i = p("Archive.07")
    assert i.family == "nn2" and i.part_number == 7


def test_r00():
    i = p("Archive.r00")
    assert i.family == "r" and i.part_number == 0


def test_inline_part():
    i = p("Yandex-EDA-Part-01-SQL.zip")
    assert i.family == "part" and i.part_number == 1 and i.container == "zip"
    assert i.base.replace(" ", "").replace("-", "").lower() == "yandexedasql"


def test_date_tail_not_a_part():
    i = p("report 2024.01.02.zip")
    assert i.part_number is None


def test_year_tail_not_a_part():
    i = p("file.1985.zip")
    assert i.part_number is None


def test_plain_file():
    i = p("Udemy_C_Programming (1).pdf")
    assert i.part_number is None and i.container == "pdf"


def test_single_char_part_base():
    i = p("A.zip.007")
    assert i.base == "A" and i.part_number == 7 and i.container == "zip"


def test_empty():
    i = p("")
    assert i.part_number is None and i.base == ""
