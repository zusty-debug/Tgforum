#!/usr/bin/env python3
"""Seed a synthetic demo DB (no Telegram involved) for offline pipeline demos.

    python scripts/demo_seed.py [db_path]     (default: data/demo.db)

Produces ~300 files: multi-part datasets in many conventions (incl. missing
and mid-range parts), country/domain/ULP single files, exact + re-upload
duplicates, quarantine names, bare-part unresolvables and mixed-convention
review cases.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from organizer.db import DB
from organizer.models import SourceFile
from organizer.normalize import prepare_file
from organizer.partparser import parse_filename

random.seed(42)
CHAT = -1001
T0 = 1_700_000_000


def mk(mid, name, size, date, fid, caption=None):
    f = SourceFile(message_id=mid, chat_id=CHAT, file_id=fid, filename=name,
                   size=size, date=date, mime="application/octet-stream",
                   caption=caption, media_group_id=None)
    f.part = parse_filename(name)
    prepare_file(f)
    return f


def main(db_path: str):
    db = DB(db_path)
    db.conn.execute("DELETE FROM source_messages")
    db.conn.commit()
    specs: list[SourceFile] = []
    mid = 1
    date = T0

    dataset_names = [
        "Customer Database 2024", "FB Export India 850Million",
        "IMEI Master DB", "Telegram Users 2023", "WhatsApp Chats UK",
        "Yandex EDA SQL", "Gmail Archive Germany", "PayPal TX Brazil",
        "VK Data 2022", "Discord Messages", "Oculus Store DB",
        "Binance Trades", "Coinbase Accounts", "Ebay Listings",
        "Amazon Orders 2023", "Booking Reviews", "Airbnb Listings FR",
        "TikTok Accounts", "Twitter X Data", "Reddit Posts 2024",
        "Steam Profiles", "Epic Games DB", "Riot LoL Data",
        "PUBG Players", "Fortnite Accounts", "EA Sports DB",
        "PlayStation ID", "Xbox Accounts", "Nintendo eShop",
        "Roblox Users", "Epic Store TX", "Zara Orders", "H&M Customers",
        "Nike Members", "Adidas DB", "Boots UK Data", "Tesco Loyalty",
        "ASDA Basket", "Sainsburys TX", "Aldi Customers", "Lidl Orders",
        "Carrefour FR", "Decathlon FR",
    ]
    conventions = [
        "part{p:02d}.rar", "part{p}.rar", "{nn:03d}", "zip.{nn:03d}",
        "z{p:02d}", "7z.{nn:03d}", "r{p:02d}", "vol{p}.iso",
    ]
    for di, ds in enumerate(dataset_names):
        conv = conventions[di % len(conventions)]
        nparts = random.randint(2, 14)
        part_size = random.randint(800_000_000, 5_200_000_000)
        date += random.randint(300, 3600)
        base = f"{ds} split{di + 1}" if conv in ("{nn:03d}",) else ds
        if conv == "z{p:02d}":
            files = [f"{base}.z{p:02d}" for p in range(1, nparts + 1)]
            files.append(f"{base}.zip")
        elif conv == "r{p:02d}":
            files = [f"{base}.r{p:02d}" for p in range(0, nparts)]
            files.append(f"{base}.rar")
        elif conv == "vol{p}.iso":
            files = [f"{base} vol{p}.iso" for p in range(1, nparts + 1)]
        elif conv == "zip.{nn:03d}":
            files = [f"{base}.zip.{p:03d}" for p in range(1, nparts + 1)]
        elif conv == "7z.{nn:03d}":
            files = [f"{base}.7z.{p:03d}" for p in range(1, nparts + 1)]
        elif conv == "{nn:03d}":
            files = [f"{base}.{p:03d}" for p in range(1, nparts + 1)]
        else:
            fmt = conv.replace("{p:02d}", "{p:02d}").replace("{p}", "{p}")
            files = [f"{base}." + fmt.format(p=p, nn=p) for p in range(1, nparts + 1)]
        # missing parts (~10%) or mid-range start (~5%)
        if nparts >= 4 and random.random() < 0.10:
            drop = random.randint(2, nparts - 1)
            files = [f for f in files if f" {drop}" not in f and f"{drop:02d}" not in f and f".{drop}." not in f and f".{drop:03d}" not in f and f"{drop}." not in f]
            if len(files) == nparts + (1 if conv in ("z{p:02d}", "r{p:02d}") else 0):
                files.pop(drop if drop < len(files) else 0)
        if nparts >= 5 and random.random() < 0.05:
            files = files[2:]
        for fn in files:
            specs.append(mk(mid, fn, part_size + random.randint(-20, 20), date,
                            f"fid-{mid}"))
            mid += 1
            date += random.randint(5, 60)

    countries = ["India", "United Kingdom", "Germany", "Brazil", "France",
                 "United States", "Canada", "Australia", "Spain", "Mexico",
                 "Turkey", "UAE", "Italy", "Poland", "Netherlands"]
    topics = ["care vision", "bank statements", "loyalty data", "orders 2023",
              "customers", "invoices", "shipping list", "crm export"]
    fmts = ["xlsx", "csv", "zip", "pdf", "txt"]
    for i in range(90):
        c = random.choice(countries)
        t = random.choice(topics)
        ext = random.choice(fmts)
        name = f"{c} {t} v{random.randint(1, 4)}.{ext}"
        date += random.randint(20, 900)
        specs.append(mk(mid, name, random.randint(500_000, 400_000_000), date,
                        f"fid-{mid}"))
        mid += 1

    services = ["yandex", "udemy", "facebook", "google", "amazon"]
    svc_topics = ["EDA", "C programming", "Data Science", "Web Dev Bootcamp",
                  "ML Specialization", "SQL Course", "API keys", "pixels",
                  "ads data", "analytics"]
    for i in range(40):
        s = random.choice(services)
        t = random.choice(svc_topics)
        name = f"{s} {t} {random.randint(2019, 2024)}.zip"
        date += random.randint(20, 900)
        specs.append(mk(mid, name, random.randint(1_000_000, 900_000_000), date,
                        f"fid-{mid}"))
        mid += 1

    for i in range(18):
        name = f"ULP update {random.randint(2021, 2024)} v{random.randint(1, 9)}.zip"
        date += random.randint(20, 900)
        specs.append(mk(mid, name, random.randint(5_000_000, 900_000_000), date,
                        f"fid-{mid}"))
        mid += 1

    for i in range(25):
        name = f"misc file {i + 1:02d} ({random.choice(['notes','stuff','items','batch'])}).bin"
        date += random.randint(20, 900)
        specs.append(mk(mid, name, random.randint(10_000, 200_000_000), date,
                        f"fid-{mid}"))
        mid += 1

    # exact duplicates (same file_id re-upload)
    for _ in range(8):
        src = random.choice(specs[:200])
        date += 120
        specs.append(mk(mid, src.filename, src.size, date, src.file_id))
        mid += 1
    # re-upload noise names, same size
    for _ in range(8):
        src = random.choice([s for s in specs[:200] if not s.part.has_part()])
        suffix = random.choice([" Copy", " (1)", "_backup", " (2)"])
        stem, ext = src.filename.rsplit(".", 1)
        date += 120
        specs.append(mk(mid, f"{stem}{suffix}.{ext}", src.size, date, f"fid-{mid}"))
        mid += 1

    for name in ["breached_users_2023.zip", "admin_passwords.txt",
                 "leaked_creds dump.rar", "pii customer list.csv"]:
        date += 300
        specs.append(mk(mid, name, random.randint(500_000, 50_000_000), date,
                        f"fid-{mid}"))
        mid += 1

    for n in range(3):
        for p in (1, 2, 3):
            date += 30
            specs.append(mk(mid, f"part{p}.rar", 700_000_000, date, f"fid-{mid}"))
            mid += 1

    for f in specs:
        db.upsert_source_message(
            chat_id=CHAT, message_id=f.message_id, file_id=f.file_id,
            filename=f.filename, extension="", mime_type=f.mime, size=f.size,
            date=f.date, caption=f.caption, media_group_id=f.media_group_id,
            media_type="document", link="")
    db.conn.commit()
    print(f"seeded {len(specs)} files → {db_path}")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/demo.db")
