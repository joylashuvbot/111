"""
Bir martalik tuzatish skripti.

Muammo: ba'zi restoranlar Google Maps "place" havolasi (masalan
.../maps/place/Caravan+Restaurant,+317+N+Schmidt+Rd,+Bolingbrook,+IL+60440/data=!4m2!3m1!1s0x...)
bilan qo'shilgan. Bu turdagi havolada lat/lng ochiq yozilmagani uchun
parse_gmaps_link() None qaytargan va kod "city" maydonidagi umumiy
shahar nomiga (masalan "Chicago, IL") tushib ketgan — natijada
restoran haqiqiy manzili o'rniga shahar markazi koordinatasi bilan
saqlanib qolgan.

Bu skript:
1) DB dagi barcha joylarni o'qiydi
2) text_channel ichidan asl Google Maps havolasini topadi (📍 <a href='...'>)
3) O'sha havolani extract_place_address_from_gmaps_url() bilan tekshiradi
4) Agar u "shahar markazi" koordinatasidan analog uzoq/farqli manzilga
   geokodlansa (ya'ni parse_gmaps_link muvaffaqiyatsiz bo'lgan holat),
   yangi, aniqroq lat/lng bilan bazani yangilaydi.

ISHLATISHDAN OLDIN: DATABASE_URL to'g'ri sozlanganiga ishonch hosil qiling
va, xohlasangiz, avval --dry-run bilan sinab ko'ring.
"""
import asyncio
import os
import re
import sys
import argparse
from urllib.parse import unquote

import asyncpg
import requests

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:EuYdfdXvtJFcPxWlxcOjQHITnxUYOtlX@trolley.proxy.rlwy.net:46504/railway",
)


def extract_gmaps_link(text_channel: str) -> str | None:
    m = re.search(r"📍\s*<a href='([^']+)'>", text_channel)
    if m:
        return m.group(1)
    m = re.search(r'📍\s*<a href="([^"]+)">', text_channel)
    return m.group(1) if m else None


def extract_place_address_from_gmaps_url(url: str) -> str | None:
    try:
        m = re.search(r"/maps/place/([^/]+)/", url)
        if not m:
            return None
        addr = unquote(m.group(1)).replace("+", " ").strip()
        return addr or None
    except Exception:
        return None


def parse_gmaps_link(url: str) -> tuple[float | None, float | None]:
    try:
        if "maps.app.goo.gl" in url or "goo.gl" in url:
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
                url = resp.url
            except Exception:
                pass
        url = unquote(url)
        patterns = [
            r"@(-?\d{1,3}\.?\d*)\s*,\s*(-?\d{1,3}\.?\d*)",
            r"!3d(-?\d+\.?\d*)!4d(-?\d+\.?\d*)",
            r"data=[^&]*!3d(-?\d+\.?\d*)!4d(-?\d+\.?\d*)",
            r"[?&]ll=(-?\d{1,3}\.?\d*)\s*,\s*(-?\d{1,3}\.?\d*)",
            r"[?&]q=(-?\d{1,3}\.?\d*)\s*,\s*([+-]?\d{1,3}\.?\d*)",
            r"/search/(-?\d{1,3}\.?\d*)\s*,\s*[+]?\s*(-?\d{1,3}\.?\d*)",
            r"@(-?\d{1,3}\.?\d*)\s*,\s*(-?\d{1,3}\.?\d*),\d+\.?\d*z",
            r"[?&]cbll=(-?\d{1,3}\.?\d*)\s*,\s*(-?\d{1,3}\.?\d*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                lat, lng = float(match.group(1)), float(match.group(2))
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    return lat, lng
    except Exception:
        pass
    return None, None


async def geocode_address(query: str):
    """Photon (ochiq, registratsiyasiz) orqali manzilni geokodlaydi."""
    import aiohttp

    url = "https://photon.komoot.io/api"
    params = {"q": query, "limit": 1}
    try:
        async with aiohttp.ClientSession() as ses:
            async with ses.get(url, params=params, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data["features"]:
                        lon, lat = data["features"][0]["geometry"]["coordinates"]
                        return lat, lon
    except Exception as e:
        print(f"  ⚠️ geokodlashda xato: {e}")
    return None, None


async def main(dry_run: bool):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, lat, lng, text_channel FROM places")

    print(f"Jami {len(rows)} ta restoran tekshirilmoqda...\n")

    fixed = 0
    for row in rows:
        link = extract_gmaps_link(row["text_channel"])
        if not link:
            continue

        # Havoladan bevosita lat/lng chiqadimi (masalan q=lat,lng)?
        direct_lat, direct_lng = parse_gmaps_link(link)
        if direct_lat is not None:
            continue  # Bu yozuv allaqachon to'g'ri, aniq koordinata bilan

        # Bevosita chiqmasa — demak bu "place/CID" turidagi havola,
        # va bazadagi lat/lng, ehtimol, umumiy shahar nomidan kelgan.
        addr = extract_place_address_from_gmaps_url(link)
        if not addr:
            print(f"[{row['id']}] {row['name']}: manzil havoladan chiqmadi, o'tkazib yuboriladi")
            continue

        new_lat, new_lng = await geocode_address(addr)
        if new_lat is None:
            print(f"[{row['id']}] {row['name']}: '{addr}' geokodlanmadi, o'tkazib yuboriladi")
            continue

        print(f"[{row['id']}] {row['name']}")
        print(f"    eski: {row['lat']}, {row['lng']}")
        print(f"    yangi ({addr}): {new_lat}, {new_lng}")

        if not dry_run:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE places SET lat = $1, lng = $2 WHERE id = $3",
                    new_lat, new_lng, row["id"],
                )
        fixed += 1

    await pool.close()
    mode = "(DRY-RUN, bazaga yozilmadi)" if dry_run else "(bazaga yozildi)"
    print(f"\nJami tuzatilgan yozuvlar: {fixed} {mode}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Bazaga yozmasdan faqat ko'rsatadi")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
