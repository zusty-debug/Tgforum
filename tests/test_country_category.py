from organizer.category import classify, detect_domain, is_ulp
from organizer.country import detect, flag


def test_uk_full_name():
    r = detect("Care_Vision_United_Kingdom.zip")
    assert r.code == "gb" and r.confidence >= 0.9


def test_uk_short_code():
    r = detect("UK customers 2024.xlsx")
    assert r.code == "gb" and r.confidence >= 0.75


def test_tld_ru():
    r = detect("eda.yandex.ru CSV.zip")
    assert r.code == "ru" and r.confidence >= 0.7


def test_city_signals_stay_weak():
    r = detect("Almaty Tashkent Karaganda leads.xls")
    assert r.confidence < 0.75  # cities alone must not name a topic


def test_multi_signal_boost():
    r = detect("India customer list mumbai export.csv")
    assert r.code == "in" and r.confidence >= 0.75


def test_bare_in_is_weak():
    r = detect("data in 2024.csv")
    assert r.confidence < 0.75


def test_unknown():
    r = detect("randomfile.bin")
    assert r.code is None and r.confidence == 0.0


def test_flag_emoji():
    assert flag("gb") == "🇬🇧"
    assert flag("in") == "🇮🇳"
    assert flag(None) == "🌐"


def test_ulp_detection():
    assert is_ulp("ULP India 2024.zip", ["ulp"])
    assert is_ulp("update ULPS final.rar", ["ulp"])
    assert not is_ulp("NoUpdate final.zip", ["ulp"])


def test_categories():
    assert classify("Udemy_C_Programming.pdf", False)[0] == "Courses / Education"
    assert classify("IMEI dataset.zip", True)[0] == "Databases / Datasets"
    assert classify("Mystery.bin", False)[0] == "Miscellaneous"


def test_domain_detection():
    r = detect_domain("eda.yandex.ru CSV.zip", [])
    assert r.name == "eda.yandex.ru"
    r = detect_domain("yandex eda csv", ["yandex"])
    assert r.name == "yandex" and r.confidence >= 0.75
    r = detect_domain("totally_random", ["yandex"])
    assert r.name is None
