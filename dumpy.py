#!/usr/bin/env python3
"""
Turbo v2 — Multi-playlist M3U generator
Genera playlist separate per categoria (italia, sport, eventi)
Supporta flussi MPD/DASH e M3U8/HLS

"""

import json
import base64
import re
import sys
import os
import gzip
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from ssl import create_default_context, CERT_NONE
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CFG = {}
raw = os.environ.get("CONFIG")
if raw:
    CFG.update(json.loads(raw))
else:
    # Fallback: load from config.json
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r") as f:
            CFG.update(json.load(f))

if not CFG:
    print("ERRORE: nessuna configurazione trovata")
    sys.exit(1)

CHROME_UA = CFG.get("STREAM_UA", "")
SKIP_DOMAINS = CFG.get("SKIP_DOMAINS", [])
SKY_CDN_PATTERNS = CFG.get("SKY_CDN_PATTERNS", [])

# Default: genera solo flussi MPD/DASH.
# Se in futuro vuoi riabilitare anche HLS/M3U8, aggiungi nel config:
# "INCLUDE_HLS": true
INCLUDE_HLS = bool(CFG.get("INCLUDE_HLS", False))

# ---------------------------------------------------------------------------
# SSL / HTTP helpers
# ---------------------------------------------------------------------------

def ssl_ctx():
    ctx = create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = CERT_NONE
    return ctx


def http_get(url, headers=None, timeout=20):
    """Simple HTTP GET, returns string."""
    hdrs = {"User-Agent": CFG.get("UA", "Mozilla/5.0")}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    try:
        with urlopen(req, timeout=timeout, context=ssl_ctx()) as r:
            return r.read().decode("utf-8", errors="replace").strip()
    except Exception as e:
        print(f"    HTTP error: {e}")
        return ""


def fetch_endpoint(endpoint, timeout=20):
    """Fetch a Mandrakodi endpoint, return parsed data."""
    url = endpoint if endpoint.startswith("http") else f"{CFG['BASE']}{endpoint}"
    req = Request(url, headers={"User-Agent": CFG.get("STREAM_USER_AGENT", CFG["UA"])})
    try:
        with urlopen(req, timeout=timeout, context=ssl_ctx()) as r:
            data = r.read().decode("utf-8", errors="replace").strip()
            if not data:
                return {}
            if data.startswith("#EXTM3U") or data.startswith("#EXTINF"):
                return {"_raw_m3u": data}
            return json.loads(data)
    except json.JSONDecodeError:
        return {"_raw_m3u": data} if data else {}
    except Exception as e:
        return {"_error": str(e)}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def clean_title(title):
    return re.sub(r'\[/?[A-Za-z0-9#]+\s*[^\]]*\]', '', title).strip()


def is_skipped(url):
    return any(d in url.lower() for d in SKIP_DOMAINS)


def is_mpd(url):
    u = url.lower()
    return ".mpd" in u or ("/dash/" in u and "/live" in u)


def is_m3u8(url):
    return ".m3u8" in url.lower()


def is_sky_cdn(url):
    return any(p in url.lower() for p in SKY_CDN_PATTERNS)


def fix_b64(s):
    return s + "=" * (-len(s) % 4)


def get_stream_headers(url):
    """Return stream headers string based on URL domain."""
    u = url.lower()
    if is_sky_cdn(url):
        return ""
    for kw_key, ref_key, orig_key in [
        ("NOWTV_KEYWORDS", "NOWTV_REFERER", "NOWTV_ORIGIN"),
        ("DAZN_KEYWORDS", "DAZN_REFERER", "DAZN_ORIGIN"),
        ("DPLAY_KEYWORDS", "DPLAY_REFERER", "DPLAY_ORIGIN"),
        ("AMSTAFF_KEYWORDS", "AMSTAFF_REFERER", "AMSTAFF_ORIGIN"),
    ]:
        if any(kw in u for kw in CFG.get(kw_key, [])):
            return f"Referer={CFG[ref_key]}&Origin={CFG[orig_key]}"
    return ""


# ---------------------------------------------------------------------------
# Channel tuple: (title, url, kid, key, headers, group, format)
#   format = "mpd" or "hls"
# ---------------------------------------------------------------------------

def make_channel(title, url, group, fmt="mpd", kid="", key="", headers=""):
    """Create a normalized channel tuple."""
    # Clean pipe from URL
    clean_url = url.split("|")[0] if "|" in url and fmt == "mpd" else url
    return (clean_title(title), clean_url, kid, key, headers, group, fmt)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def resolve_amstaff(val):
    """amstaff@@base64(url|kid:key|token) → MPD + ClearKey"""
    raw = val.removeprefix("amstaff@@")
    try:
        decoded = base64.b64decode(fix_b64(raw)).decode()
    except Exception:
        decoded = raw

    parts = decoded.split("|")
    mpd_url = parts[0]
    kid, key = "", ""
    if len(parts) >= 2 and parts[1] not in ("0:0", "0000", "0", ""):
        if ":" in parts[1]:
            kid, key = parts[1].split(":", 1)
    token = parts[2] if len(parts) >= 3 and parts[2] else ""

    headers = get_stream_headers(mpd_url)
    if token:
        headers = f"dazn-token={token}&{headers}" if headers else f"dazn-token={token}"

    return mpd_url, kid, key, headers


def resolve_sky(ch_id):
    """sky@@channel → MPD + ClearKey via API + XOR decrypt"""
    url = CFG["STREAM_RESOLVE_URL"] + ch_id
    req = Request(url, headers={"User-Agent": CFG["STREAM_USER_AGENT"]})
    try:
        with urlopen(req, timeout=15, context=ssl_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace").strip()
        data = json.loads(raw)
        if "data" not in data:
            return None
        # XOR decrypt
        enc = base64.b64decode(data["data"])
        xor_key = CFG["XOR_SECRET"].encode()
        dec = bytearray(enc[i] ^ xor_key[i % len(xor_key)] for i in range(len(enc)))
        result = json.loads(dec.decode("utf-8"))
        return result["manifest"], result["kid"], result["key"]
    except Exception as e:
        print(f"    Sky resolve error: {e}")
        return None


def resolve_sky_tv(ch_id):
    """skyTV@@channel → streaming URL via Sky API"""
    url = f"{CFG['SKY_TV_API']}?id={ch_id}&isMobile=false"
    try:
        data = json.loads(http_get(url))
        return data.get("streaming_url")
    except:
        return None


def resolve_daddy_code(code):
    """daddyCode@@857 → M3U8 URL via DaddyLive scraping"""
    import requests as req_lib
    try:
        daddy_ref = CFG.get("DADDY_REFERER", "https://dlhd.pk/")
        stream_ref = CFG.get("DADDY_STREAM_REFERER", "https://donis.jimpenopisonline.online/")

        # Step 1: fetch stream page
        page_url = f"https://dlhd.pk/stream/stream-{code}.php"
        hdrs = {"User-Agent": "Mozilla/5.0", "Referer": daddy_ref}
        page = req_lib.get(page_url, headers=hdrs, timeout=15).text

        # Step 2: find iframe
        iframe = re.findall(r'<iframe src="(.*?)"', page)
        if not iframe:
            return None
        iframe_url = iframe[0]

        # Step 3: fetch iframe, find base64 encoded URL
        page2 = req_lib.get(iframe_url, headers={"User-Agent": "Mozilla/5.0", "Referer": daddy_ref}, timeout=15).text
        b64_match = re.findall(r"window\.atob\('(.*?)'\)", page2)
        if not b64_match:
            return None

        m3u8_url = base64.b64decode(b64_match[0]).decode("utf-8")

        # Build URL with headers
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 OPR/130.0.0.0"
        headers_str = f"Referer={stream_ref}&Origin={stream_ref}&User-Agent={ua}"
        return m3u8_url, headers_str
    except Exception as e:
        print(f"    DaddyCode resolve error [{code}]: {e}")
        return None


def resolve_mediahosting(stream_id):
    """mediahosting@@123 → M3U8 URL from template"""
    template = CFG.get("MEDIAHOSTING_TEMPLATE", "")
    referer = CFG.get("MEDIAHOSTING_REFERER", "https://mediahosting.space/")
    url = template.replace("{id}", str(stream_id))
    headers_str = f"Referer={referer}&Origin={referer}"
    return url, headers_str


def resolve_zappr():
    """zappr@@menu → Canali DTT italiani da API Zappr. Returns list of channel tuples."""
    channels = []
    try:
        data = json.loads(http_get("https://channels.zappr.stream/it/dtt/national.json"))
        for ch in data.get("channels", []):
            tipo = ch.get("type", "")
            if tipo not in ("hls", "dash"):
                continue
            if tipo == "hls" and not INCLUDE_HLS:
                continue

            lcn = str(ch.get("lcn", ""))
            name = ch.get("name", "?")
            url = ch.get("url", "")
            title = f"[{lcn}] {name}" if lcn else name

            if not url:
                continue

            # zappr:// → skyTV resolver
            if url.startswith("zappr://sky/"):
                sky_id = url.split("/")[-1]
                resolved = resolve_sky_tv(sky_id)
                if resolved:
                    if is_mpd(resolved):
                        channels.append(make_channel(title, resolved, "DTT Italia", "mpd",
                                                     headers=get_stream_headers(resolved)))
                    elif INCLUDE_HLS and is_m3u8(resolved):
                        channels.append(make_channel(title, resolved, "DTT Italia", "hls"))
                continue

            # zappr:// other (skip)
            if url.startswith("zappr://"):
                continue

            if tipo == "dash":
                kid, key = "", ""
                lic = ch.get("license", "")
                if lic == "clearkey":
                    ld = ch.get("licensedetails", {})
                    if isinstance(ld, dict):
                        pairs = list(ld.items())
                        if pairs:
                            kid, key = pairs[0]
                channels.append(make_channel(title, url, "DTT Italia", "mpd", kid, key))

            elif tipo == "hls":
                channels.append(make_channel(title, url, "DTT Italia", "hls"))

    except Exception as e:
        print(f"    Zappr error: {e}")

    return channels


def resolve_freeshot(code):
    """freeshot@@skysport24 → M3U8 URL with dynamic token"""
    try:
        freeshot_base = CFG.get("FREESHOT_BASE", "https://popcdn.day/player/")
        freeshot_stream = CFG.get("FREESHOT_STREAM", "https://lovely.lovetier.bz")
        freeshot_ref = CFG.get("FREESHOT_REFERER", "https://thisnot.business/")

        page = http_get(f"{freeshot_base}{code}", headers={"Referer": freeshot_ref})
        if not page:
            return None

        token_match = re.findall(r'currentToken:\s*"(.*?)"', page)
        if not token_match:
            return None

        token = token_match[0]
        m3u8_url = f"{freeshot_stream}/{code}/tracks-v1a1/mono.m3u8?token={token}"
        return m3u8_url, ""
    except Exception as e:
        print(f"    Freeshot resolve error [{code}]: {e}")
        return None


# ---------------------------------------------------------------------------
# Stream extraction — supports both MPD and M3U8
# ---------------------------------------------------------------------------

def parse_items(data):
    """Extract flat list of items from JSON response."""
    items = []
    if not isinstance(data, dict):
        return items
    for key in ("items", "channels"):
        entries = data.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                items.append(entry)
                # Also check nested items
                for subkey in ("items", "channels"):
                    subs = entry.get(subkey, [])
                    if not isinstance(subs, list):
                        continue
                    for sub in subs:
                        if isinstance(sub, dict):
                            items.append(sub)
    return items


def extract_streams(endpoint, group="Live"):
    """Extract streams from a Mandrakodi endpoint. Returns list of channel tuples."""
    data = fetch_endpoint(endpoint)
    if "_error" in data:
        return []

    channels = []

    # --- Raw M3U ---
    if "_raw_m3u" in data:
        current_title = ""
        for line in data["_raw_m3u"].split("\n"):
            line = line.strip()
            if line.startswith("#EXTINF"):
                m = re.search(r',(.+)$', line)
                current_title = m.group(1).strip() if m else ""
            elif line and not line.startswith("#"):
                if is_skipped(line):
                    current_title = ""
                    continue
                if is_mpd(line):
                    h = get_stream_headers(line)
                    channels.append(make_channel(current_title, line, group, "mpd", headers=h))
                elif INCLUDE_HLS and is_m3u8(line):
                    # Parse pipe headers from M3U8 URLs
                    url_part = line.split("|")[0]
                    h = ""
                    if "|" in line:
                        h = line.split("|", 1)[1]
                    channels.append(make_channel(current_title, url_part, group, "hls", headers=h))
                current_title = ""
        return channels

    # --- JSON items ---
    items = parse_items(data)
    for item in items:
        title = clean_title(item.get("title", ""))
        if not title or title in ("ignore", "NEXT PAGE", "TRY TO RESOLVE", "NO LINK FOUND", ""):
            continue
        if "HAVE PROBLEM" in title or "UNDER WORK" in title:
            continue

        resolved = False

        # --- Direct links ---
        for key_name in ("link", "new_link"):
            val = item.get(key_name, "")
            if not val or val in ("ignore", "ignoreme") or is_skipped(val):
                continue
            if is_mpd(val):
                h = get_stream_headers(val)
                channels.append(make_channel(title, val, group, "mpd", headers=h))
                resolved = True
                break
            elif INCLUDE_HLS and is_m3u8(val):
                url_part = val.split("|")[0]
                h = val.split("|", 1)[1] if "|" in val else ""
                channels.append(make_channel(title, url_part, group, "hls", headers=h))
                resolved = True
                break
        if resolved:
            continue

        # --- Resolvers (myresolve / externallink2) ---
        for key_name in ("myresolve", "externallink2"):
            val = item.get(key_name, "")
            if not val:
                continue

            # amstaff@@
            if val.startswith("amstaff@@"):
                mpd, kid, key, h = resolve_amstaff(val)
                if is_mpd(mpd):
                    channels.append(make_channel(title, mpd, group, "mpd", kid, key, h))
                resolved = True
                break

            # sky@@
            if val.startswith("sky@@"):
                result = resolve_sky(val.removeprefix("sky@@"))
                if result:
                    mpd, kid, key = result
                    h = get_stream_headers(mpd)
                    channels.append(make_channel(title, mpd, group, "mpd", kid, key, h))
                resolved = True
                break

            # skyTV@@
            if val.startswith("skyTV@@"):
                url = resolve_sky_tv(val.removeprefix("skyTV@@"))
                if url:
                    if is_mpd(url):
                        channels.append(make_channel(title, url, group, "mpd", headers=get_stream_headers(url)))
                    elif INCLUDE_HLS and is_m3u8(url):
                        channels.append(make_channel(title, url, group, "hls"))
                resolved = True
                break

            # daddyCode@@
            if val.startswith("daddyCode@@"):
                if INCLUDE_HLS:
                    code = val.removeprefix("daddyCode@@")
                    result = resolve_daddy_code(code)
                    if result:
                        m3u8, h = result
                        channels.append(make_channel(title, m3u8, group, "hls", headers=h))
                resolved = True
                break

            # mediahosting@@
            if val.startswith("mediahosting@@"):
                if INCLUDE_HLS:
                    stream_id = val.removeprefix("mediahosting@@")
                    m3u8, h = resolve_mediahosting(stream_id)
                    channels.append(make_channel(title, m3u8, group, "hls", headers=h))
                resolved = True
                break

            # freeshot@@
            if val.startswith("freeshot@@"):
                if INCLUDE_HLS:
                    code = val.removeprefix("freeshot@@")
                    result = resolve_freeshot(code)
                    if result:
                        m3u8, h = result
                        channels.append(make_channel(title, m3u8, group, "hls", headers=h))
                resolved = True
                break

            # zappr@@
            if val.startswith("zappr@@"):
                # Gestito separatamente tramite resolve_zappr()
                resolved = True
                break

            # risolvi@@
            if val.startswith("risolvi@@"):
                raw_url = val[len("risolvi@@"):]
                if raw_url.startswith("http") and not is_skipped(raw_url):
                    if is_mpd(raw_url):
                        channels.append(make_channel(title, raw_url, group, "mpd", headers=get_stream_headers(raw_url)))
                    elif INCLUDE_HLS and is_m3u8(raw_url):
                        url_part = raw_url.split("|")[0]
                        h = raw_url.split("|", 1)[1] if "|" in raw_url else ""
                        channels.append(make_channel(title, url_part, group, "hls", headers=h))
                resolved = True
                break

            # cdnLive@@
            if val.startswith("cdnLive@@"):
                raw_url = val[len("cdnLive@@"):]
                if raw_url.startswith("http") and not is_skipped(raw_url):
                    if is_mpd(raw_url):
                        channels.append(make_channel(title, raw_url, group, "mpd", headers=get_stream_headers(raw_url)))
                    elif INCLUDE_HLS and is_m3u8(raw_url):
                        channels.append(make_channel(title, raw_url, group, "hls"))
                resolved = True
                break

            if is_skipped(val):
                resolved = True
                break

    return channels


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def dedup_channels(channels):
    """Remove duplicate channels by URL."""
    seen = set()
    result = []
    for ch in channels:
        url_key = ch[1].split("?")[0]  # dedup by base URL
        if url_key not in seen:
            seen.add(url_key)
            result.append(ch)
    return result


# ---------------------------------------------------------------------------
# Write M3U — formato KODIPROP ottimizzato per OTT Navigator
# ---------------------------------------------------------------------------

def format_clearkey_for_ott(kid, key):
    """Formatta ClearKey in modo compatibile con OTT Navigator.

    - singola chiave: kid:key
    - chiavi multiple: {"kid1":"key1","kid2":"key2"}
    """
    raw = f"{kid}:{key}".strip()
    pairs = []
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k and v:
            pairs.append((k, v))

    if len(pairs) <= 1:
        return raw
    return json.dumps(dict(pairs), separators=(",", ":"))


def write_m3u(channels, path, epg_url=""):
    """Write playlist in KODIPROP format optimized for OTT Navigator."""
    with open(path, "w", encoding="utf-8") as f:
        header = "#EXTM3U"
        if epg_url:
            header += f' url-tvg="{epg_url}"'
        f.write(header + "\n")

        for title, url, kid, key, headers_str, group, fmt in channels:
            f.write(f'#EXTINF:-1 group-title="{group}",{title}\n')

            if fmt == "mpd":
                f.write('#KODIPROP:inputstream.adaptive.manifest_type=mpd\n')

            # ClearKey (MPD only)
            if fmt == "mpd" and kid and key:
                f.write('#KODIPROP:inputstream.adaptive.license_type=clearkey\n')
                f.write(f'#KODIPROP:inputstream.adaptive.license_key={format_clearkey_for_ott(kid, key)}\n')

            # Stream headers
            if headers_str:
                f.write(f'#KODIPROP:inputstream.adaptive.stream_headers={headers_str}\n')

            f.write(f'{url}\n')

    mpd_count = sum(1 for c in channels if c[6] == "mpd")
    hls_count = sum(1 for c in channels if c[6] == "hls")
    ck_count = sum(1 for c in channels if c[2] and c[3])
    print(f"  → {path}: {len(channels)} canali ({mpd_count} MPD, {hls_count} HLS, {ck_count} con ClearKey)")


# ---------------------------------------------------------------------------
# Unified playlist ordering
# ---------------------------------------------------------------------------

MACRO_ORDER = {
    "INTRATTENIMENTO": 0,
    "SPORT": 1,
    "EVENTI": 2,
}

LANG_ORDER = {
    "ITA": 0,
    "ITA+ESP": 1,
    "ITA+NED": 2,
    "MULTI": 3,
    "ENG": 4,
    "ESP": 5,
    "FRA": 6,
    "GER": 7,
    "POL": 8,
    "POR": 9,
    "HRV": 10,
    "SRB": 11,
    "NED": 12,
    "GRE": 13,
    "TUR": 14,
    "ARA": 15,
    "MIX": 99,
}

LANG_CODES = {
    "ITA", "ENG", "ESP", "FRA", "GER", "DEU", "POL", "POR", "BRA",
    "HRV", "CRO", "SRB", "NED", "DUT", "GRE", "ELL", "TUR", "ARA",
    "RUS", "UKR", "SWE", "NOR", "DAN", "FIN", "CZE", "SVK", "HUN",
    "ROU", "BUL", "SLO", "SLV", "ALB", "BOS", "MKD", "ISR", "HEB",
    "JPN", "KOR", "CHI", "ZHO", "IND", "HIN", "THA", "VIE",
}

LANG_NORMALIZE = {
    "DEU": "GER",
    "CRO": "HRV",
    "DUT": "NED",
    "ELL": "GRE",
    "BRA": "POR",
    "HEB": "ISR",
    "ZHO": "CHI",
    "HIN": "IND",
    "SLV": "SLO",
}

ITALIAN_ENTERTAINMENT_GROUPS = {
    "DTT Italia", "Mediaset", "Sky", "News", "Regionali", "ITA Estero",
}

ITALIAN_SPORT_GROUPS = {
    "Sky Sport", "EuroSport",
}


def detect_macro(source_playlist, group, title):
    """Return top-level category for the unified playlist."""
    if source_playlist == "eventi":
        return "EVENTI"
    if source_playlist == "sport":
        return "SPORT"
    if group in {"Italy Sports", "Sky Sport", "EuroSport", "Sport MPD", "Calcio"}:
        return "SPORT"
    return "INTRATTENIMENTO"


def detect_language(source_playlist, group, title):
    """Detect language from title tags like (ITA), (ENG), (ITA/ESP - MPD)."""
    text = title.upper()
    found = []

    for chunk in re.findall(r"\(([^)]*)\)", text):
        chunk = chunk.replace("-", "/").replace(",", "/").replace("+", "/")
        for token in re.split(r"[/\s]+", chunk):
            token = token.strip().upper()
            if token in {"MPD", "HD", "FHD", "UHD", "4K", "SD", "LIVE"}:
                continue
            if token in LANG_CODES:
                found.append(LANG_NORMALIZE.get(token, token))

    for m in re.finditer(r"\b([A-Z]{3})(?:/([A-Z]{3}))+\b", text):
        for token in m.group(0).split("/"):
            if token in LANG_CODES:
                found.append(LANG_NORMALIZE.get(token, token))

    found = list(dict.fromkeys(found))
    if found:
        if len(found) == 1:
            return found[0]
        if "ITA" in found and len(found) == 2:
            other = [x for x in found if x != "ITA"][0]
            return f"ITA+{other}"
        return "MULTI"

    if group in ITALIAN_ENTERTAINMENT_GROUPS:
        return "ITA"
    if group in ITALIAN_SPORT_GROUPS:
        return "ITA"
    if source_playlist == "eventi":
        return "MIX"
    if group in {"Italy Sports", "Sport MPD"}:
        return "MIX"
    return "MIX"


def normalize_title_for_sort(title):
    parts = re.split(r"(\d+)", title.upper())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def unified_group(source_playlist, group, title):
    macro = detect_macro(source_playlist, group, title)
    lang = detect_language(source_playlist, group, title)
    return macro, lang, f"{macro} | {lang} | {group or 'Altro'}"


def unified_sort_key(item):
    source_playlist, channel = item
    title, url, kid, key, headers_str, group, fmt = channel
    macro, lang, new_group = unified_group(source_playlist, group, title)
    return (
        MACRO_ORDER.get(macro, 99),
        LANG_ORDER.get(lang, 50),
        new_group.upper(),
        normalize_title_for_sort(title),
        source_playlist,
    )


def build_unified_channels(source_channels):
    """Convert (source_playlist, channel) entries into regular channel tuples."""
    unified = []
    for source_playlist, channel in source_channels:
        title, url, kid, key, headers_str, group, fmt = channel
        macro, lang, new_group = unified_group(source_playlist, group, title)
        unified.append((title, url, kid, key, headers_str, new_group, fmt))
    return unified


# ---------------------------------------------------------------------------
# EPG
# ---------------------------------------------------------------------------

def download_epg(epg_path):
    try:
        req = Request(CFG["EPG_SOURCE_URL"], headers={"User-Agent": CFG["UA"]})
        with urlopen(req, timeout=120, context=ssl_ctx()) as r:
            gz_data = r.read()
        xml = gzip.decompress(gz_data).decode("utf-8")
        with open(epg_path, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"  EPG scaricato: {len(xml)} bytes")
        return True
    except Exception:
        print("  EPG non disponibile")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playlists")
    os.makedirs(out_dir, exist_ok=True)
    epg_path = os.path.join(out_dir, "epg.xml")

    print("=== DUMPY — Unified MPD playlist for OTT Navigator ===\n")

    playlists = CFG.get("PLAYLISTS", {})
    source_channels = []

    # Rimuove vecchie playlist separate generate dalle versioni precedenti.
    # La playlist finale supportata ora è solo playlists/dumpy.m3u.
    for old_name in playlists.keys():
        old_path = os.path.join(out_dir, f"{old_name}.m3u")
        if os.path.exists(old_path):
            os.remove(old_path)

    for playlist_name, playlist_cfg in playlists.items():
        desc = playlist_cfg.get("description", "")
        endpoints = playlist_cfg.get("endpoints", {})

        print(f"\n{'─' * 60}")
        print(f"📥 Sorgente {playlist_name} — {desc}")
        print(f"{'─' * 60}")

        playlist_count = 0
        for ep_name, (ep_code, ep_group) in endpoints.items():
            print(f"  {ep_name} [{ep_code}]...", end=" ", flush=True)

            # Special resolvers
            if ep_code == "ZAPPR":
                streams = resolve_zappr()
            else:
                streams = extract_streams(ep_code, ep_group)

            print(f"{len(streams)} canali")
            playlist_count += len(streams)
            source_channels.extend((playlist_name, stream) for stream in streams)

        print(f"  Totale sorgente {playlist_name}: {playlist_count} canali")

    source_channels.sort(key=unified_sort_key)
    unified_channels = build_unified_channels(source_channels)

    m3u_path = os.path.join(out_dir, "dumpy.m3u")
    print(f"\n{'─' * 60}")
    print("🧩 Playlist unica ordinata")
    write_m3u(unified_channels, m3u_path, epg_url="epg.xml")

    # EPG
    print(f"\n{'─' * 60}")
    print("📡 EPG")
    download_epg(epg_path)

    print(f"\n{'=' * 60}")
    print(f"✅ FATTO! {len(unified_channels)} canali totali in playlists/dumpy.m3u")
    print(f"   Output: {out_dir}/")
    print(f"{'=' * 60}")
