#!/usr/bin/env python3
"""CROUS yeni oda alarmı.

trouverunlogement.lescrous.fr sitesinde Paris ve çevresinde yeni bir oda
listelendiğinde ntfy ile telefona bildirim gönderir.

Ortam değişkenleri:
  NTFY_TOPIC  : ntfy konu adı (zorunlu)
  STATE_FILE  : daha önce görülen odaların tutulduğu dosya (varsayılan: seen.json)
  CROUS_TOOL_ID : sitenin yıl kodu (varsayılan: 48, 2026-2027)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://trouverunlogement.lescrous.fr"
TOOL_ID = os.environ.get("CROUS_TOOL_ID", "48")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))

# Arama alanı: Paris ve çevresi (batı, kuzey, doğu, güney)
WEST, NORTH, EAST, SOUTH = 1.95, 49.10, 2.75, 48.60

# Île-de-France departmanları; ilk dördü okula en yakın olanlar
IDF_DEPTS = {"75", "77", "78", "91", "92", "93", "94", "95"}
PRIORITY_DEPTS = {"75", "92", "93", "94"}

ERROR_NOTIFY_EVERY = 6 * 3600  # aynı hata için en fazla 6 saatte bir uyar
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------- yardımcılar

def dept_of(postal):
    return postal[:2] if postal and len(postal) == 5 else ""


def find_postal(text):
    match = re.search(r"\b(\d{5})\b", text or "")
    return match.group(1) if match else ""


def room_url(room_id):
    return f"{BASE}/tools/{TOOL_ID}/accommodations/{room_id}"


# ------------------------------------------------------- 1. yol: sitenin API'si

def _iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_dicts(value)


def _price_text(item):
    prices = []
    for mode in item.get("occupationModes") or []:
        rent = mode.get("rent") if isinstance(mode, dict) else None
        if isinstance(rent, dict):
            for key in ("min", "max"):
                value = rent.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    # API kuruş cinsinden de verebiliyor
                    prices.append(value / 100 if value > 5000 else value)
    if not prices:
        return ""
    low, high = min(prices), max(prices)
    return f"{low:.0f} €" if low == high else f"{low:.0f}–{high:.0f} €"


def parse_api(data):
    """API cevabından oda listesini çıkarır."""
    items = None
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, dict) and isinstance(results.get("items"), list):
            items = results["items"]
    if items is None:  # beklenmedik yapı: id + residence içeren her şeyi topla
        items = [d for d in _iter_dicts(data) if "id" in d and "residence" in d]

    rooms = {}
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            continue
        residence = item.get("residence") if isinstance(item.get("residence"), dict) else {}
        address = residence.get("address") or item.get("address") or ""
        if isinstance(address, dict):
            address = " ".join(str(v) for v in address.values() if v)
        name = residence.get("label") or item.get("label") or "Oda"
        room_id = str(item["id"])
        rooms[room_id] = {
            "id": room_id,
            "name": str(name).strip(),
            "address": str(address).strip(),
            "price": _price_text(item),
            "postal": find_postal(str(address)),
        }
    return list(rooms.values())


def fetch_api():
    payload = {
        "idTool": int(TOOL_ID),
        "need_aggregation": False,
        "page": 1,
        "pageSize": 100,
        "sector": None,
        "occupationModes": [],
        "location": [{"lon": WEST, "lat": NORTH}, {"lon": EAST, "lat": SOUTH}],
        "residence": None,
        "precision": 6,
        "equipment": [],
        "price": {"max": 10000000},
        "area": {"min": 0},
        "adaptedPmr": False,
        "toolMechanism": "residual",
    }
    response = requests.post(
        f"{BASE}/api/fr/search/{TOOL_ID}",
        json=payload,
        headers={**HEADERS, "Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "results" not in data:
        raise ValueError("API cevabı beklenen yapıda değil")
    return parse_api(data)


# ------------------------------------------- 2. yol: arama sayfasının HTML'i

LINK_RE = re.compile(r'href="(?:https://trouverunlogement\.lescrous\.fr)?/tools/\d+/accommodations/(\d+)"')
TAG_RE = re.compile(r"<[^>]+>")


def _clean(html_fragment):
    html_fragment = re.sub(r"<[^>]*$", "", html_fragment)  # yarım kalmış etiket
    text = TAG_RE.sub(" ", html_fragment)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = text.replace("&amp;", "&").replace("&#039;", "'").replace("&euro;", "€")
    return re.sub(r"\s+", " ", text).strip()


def parse_html(html):
    """Arama sonuç sayfasından oda listesini çıkarır."""
    lowered = html.lower()
    page_ok = "logement" in lowered and ("trouvé" in lowered or "trouve" in lowered)
    rooms = {}
    matches = list(LINK_RE.finditer(html))
    for index, match in enumerate(matches):
        room_id = match.group(1)
        if room_id in rooms:
            continue
        start = html.find(">", match.end()) + 1 or match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else start + 1500
        snippet = _clean(html[start:min(end, start + 1500)])
        price = re.search(r"(\d[\d\s,.]*€(?:\s*à\s*\d[\d\s,.]*€)?)", snippet)
        rooms[room_id] = {
            "id": room_id,
            "name": snippet[:60] or "Oda",
            "address": snippet[:160],
            "price": price.group(1).strip() if price else "",
            "postal": find_postal(snippet),
        }
    if not rooms and not page_ok:
        raise ValueError("Arama sayfası okunamadı (yapı değişmiş olabilir)")
    return list(rooms.values())


def fetch_html():
    url = f"{BASE}/tools/{TOOL_ID}/search?bounds={WEST}_{NORTH}_{EAST}_{SOUTH}"
    response = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=30)
    response.raise_for_status()
    return parse_html(response.text)


# ----------------------------------------------------------- bildirim, durum

def notify(title, message, click=None, priority=3, tags=None):
    if not NTFY_TOPIC:
        print(f"[ntfy yok] {title}: {message}")
        return
    body = {"topic": NTFY_TOPIC, "title": title, "message": message,
            "priority": priority, "tags": tags or ["house"]}
    if click:
        body["click"] = click
    try:
        requests.post("https://ntfy.sh/", json=body, timeout=20).raise_for_status()
    except requests.RequestException as exc:
        print(f"Bildirim gönderilemedi: {exc}", file=sys.stderr)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": [], "initialized": False, "last_error_notify": 0}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def in_area(room):
    dept = dept_of(room["postal"])
    return not dept or dept in IDF_DEPTS  # posta kodu bulunamazsa yine de bildir


def describe(room):
    parts = [room["address"] or room["name"]]
    if room["price"]:
        parts.append(room["price"])
    return " · ".join(parts)


# ------------------------------------------------------------------- ana akış

def get_rooms():
    errors = []
    for method in (fetch_api, fetch_html):
        try:
            rooms = method()
            print(f"{method.__name__}: {len(rooms)} oda bulundu")
            return rooms
        except Exception as exc:  # bir yol çökerse diğerini dene
            errors.append(f"{method.__name__}: {exc}")
            print(errors[-1], file=sys.stderr)
    raise RuntimeError(" | ".join(errors))


def main():
    state = load_state()
    now = time.time()
    try:
        rooms = [room for room in get_rooms() if in_area(room)]
    except RuntimeError as exc:
        if now - state.get("last_error_notify", 0) > ERROR_NOTIFY_EVERY:
            notify("⚠️ CROUS alarmı siteyi okuyamadı",
                   f"Kontrol başarısız oldu, ayrıntı GitHub Actions kaydında. {str(exc)[:200]}",
                   priority=2, tags=["warning"])
            state["last_error_notify"] = now
        save_state(state)
        return

    seen = set(state["seen"])
    new_rooms = [room for room in rooms if room["id"] not in seen]

    if not state.get("initialized"):
        listing = "\n".join(f"• {describe(r)}" for r in rooms[:5]) or "Şu an bölgede boş oda yok."
        notify("✅ CROUS alarmı çalışıyor",
               f"Paris ve çevresinde şu an {len(rooms)} oda listede. Yeni oda çıkınca haber vereceğim.\n{listing}",
               click=f"{BASE}/tools/{TOOL_ID}/search", tags=["white_check_mark"])
        state["initialized"] = True
    else:
        for room in new_rooms:
            near = dept_of(room["postal"]) in PRIORITY_DEPTS
            notify(f"{'🔥' if near else '🏠'} Yeni CROUS odası ({room['postal'] or '?'})",
                   describe(room) + "\nTarihleri 15.01.2027 – 03.07.2027 olarak dene ve hemen başvur.",
                   click=room_url(room["id"]), priority=5 if near else 4)

    state["seen"] = state["seen"] + [r["id"] for r in rooms if r["id"] not in seen]
    state["last_error_notify"] = 0
    save_state(state)


if __name__ == "__main__":
    main()
