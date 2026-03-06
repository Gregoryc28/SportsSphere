import asyncio
import logging
import time
import base64
import json
import hmac
import hashlib
import os
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from quart import Quart, jsonify, send_from_directory, url_for, request, Response
from quart_cors import cors
from playwright.async_api import async_playwright

# --- NEW: Impersonation Library for the Proxy ---
from curl_cffi import requests as cffi_requests

# --- Configuration ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Quart(__name__)
app = cors(app, allow_origin="*")

API_BASE = "https://streamed.pk/api"
STREAMED_HOST = "https://streamed.pk"
CACHE_TIMEOUT = 300
STREAM_CACHE_DURATION = 900
SECRET_KEY = os.environ.get("PROXY_SECRET_KEY", "change-me-to-a-real-secret")

# Global Caches
catalog_cache = {}
stream_cache = {}

# --- Helper Functions ---


def sign_url(url: str) -> str:
    """Generate an HMAC-SHA256 signature for a proxy URL."""
    return hmac.new(SECRET_KEY.encode(), url.encode(), hashlib.sha256).hexdigest()


async def get_all_matches() -> List[Dict]:
    global catalog_cache
    current_time = time.time()

    if catalog_cache and (
        current_time - catalog_cache.get("last_updated", 0) < CACHE_TIMEOUT
    ):
        return catalog_cache["data"]

    try:
        resp = requests.get(f"{API_BASE}/matches/all-today", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        data.sort(key=lambda x: x.get("date", 0))
        catalog_cache = {"last_updated": current_time, "data": data}
        return data
    except Exception as e:
        logging.error(f"Failed to fetch matches: {e}")
        return catalog_cache.get("data", [])


def get_poster_url(match: Dict) -> str:
    poster_val = match.get("poster")
    if not poster_val:
        return None
    if poster_val.startswith("http"):
        return poster_val
    if poster_val.startswith("/"):
        return f"{STREAMED_HOST}{poster_val}.webp"
    return f"{API_BASE}/images/proxy/{poster_val}.webp"


async def get_all_stream_embeds(match_id: str) -> List[Dict]:
    matches = await get_all_matches()
    match = next((m for m in matches if m["id"] == match_id), None)

    if not match or not match.get("sources"):
        return []

    found_embeds = []

    for src in match["sources"]:
        source_name = src["source"]
        source_id = src["id"]
        try:
            api_url = f"{API_BASE}/stream/{source_name}/{source_id}"
            resp = requests.get(api_url, timeout=5)
            if resp.status_code == 200:
                streams_data = resp.json()
                for stream_obj in streams_data:
                    embed_url = stream_obj.get("embedUrl")
                    if not embed_url:
                        continue

                    quality = "HD" if stream_obj.get("hd") else "SD"
                    stream_no = stream_obj.get("streamNo", 1)
                    label = f"{source_name.title()} - Stream {stream_no} ({quality})"

                    found_embeds.append(
                        {"label": label, "source": source_name, "embed_url": embed_url}
                    )
        except Exception:
            continue

    # --- Priority Sorting: fastest/most reliable sources first ---
    def _embed_sort_key(embed):
        source = embed.get("source", "").lower()
        if "golf" in source:
            return 0
        if "admin" in source:
            return 1
        if "delta" in source:
            return 2
        if "delta" in source:
            return 3
        return 4

    found_embeds.sort(key=_embed_sort_key)
    return found_embeds


async def resolve_with_playwright(embed_url: str, browser) -> Optional[Dict]:
    """Resolve an embed URL using a shared browser instance."""
    logging.info(f"Resolving Embed: {embed_url}")
    stream_info = {}

    parsed_uri = urlparse(embed_url)
    clean_root = "{uri.scheme}://{uri.netloc}/".format(uri=parsed_uri)
    clean_origin = "{uri.scheme}://{uri.netloc}".format(uri=parsed_uri)

    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    context = await browser.new_context(user_agent=user_agent)
    page = await context.new_page()

    # --- Resource Blocking: Only load HTML/JS, skip everything else ---
    BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media", "other"}

    async def block_unnecessary_resources(route):
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", block_unnecessary_resources)

    async def handle_popup(popup):
        if popup != page:
            try:
                await popup.close()
            except:
                pass

    context.on("page", handle_popup)

    async def handle_request(request):
        # Atomic Check: If we already have the URL, stop processing
        if "url" in stream_info:
            return

        if ".m3u" in request.url and "http" in request.url:
            if "narakathegame" in request.url:
                return

            logging.info(f"SUCCESS! Found Stream Candidate: {request.url}")
            try:
                # 1. CRITICAL: Grab headers BEFORE setting stream_info['url']
                # This prevents the browser from closing until we have the data.
                headers = await request.all_headers()
                cookies = headers.get("cookie", headers.get("Cookie", ""))

                actual_referer = headers.get("referer", embed_url)
                final_headers = {
                    "User-Agent": user_agent,
                    "Cookie": cookies,
                    "Referer": actual_referer,
                    "Origin": clean_origin,
                }

                if "strmd" in request.url or "delta" in request.url:
                    final_headers["Referer"] = clean_root
                    if "Origin" in final_headers:
                        del final_headers["Origin"]

                # 2. SAVE DATA
                stream_info["headers"] = final_headers
                stream_info["clean_root"] = clean_root

                # 3. SET URL LAST (This signals the main loop that we are done)
                stream_info["url"] = request.url

            except Exception as e:
                logging.warning(f"Could not grab headers (Retrying...): {e}")

    page.on("request", handle_request)

    try:
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=25000)

        for i in range(5):
            # Check if we found it (headers are guaranteed to be there if URL is set)
            if "url" in stream_info:
                break

            try:
                await page.mouse.click(500, 300)
            except:
                pass
            await asyncio.sleep(1.0)

            target = page
            for frame in page.frames:
                if (
                    "embed" in frame.url
                    or "poo" in frame.url
                    or "exposestrat" in frame.url
                    or "maestro" in frame.url
                ):
                    target = frame

            buttons = [
                "button.vjs-big-play-button",
                ".play-button",
                "div.play",
                "svg",
                "video",
                "#player",
                ".jw-icon-playback",
            ]
            for btn in buttons:
                try:
                    if (
                        await target.locator(btn).count() > 0
                        and await target.locator(btn).first.is_visible()
                    ):
                        await target.locator(btn).first.click(timeout=500)
                except:
                    pass
            await asyncio.sleep(1.5)

    except Exception as e:
        logging.error(f"Playwright error: {e}")
    finally:
        await context.close()

    return stream_info


# --- THE PROXY ENGINE ---


@app.route("/proxy")
async def proxy_stream():
    target_url = request.args.get("url")
    headers_b64 = request.args.get("headers")
    sig = request.args.get("sig")

    if not target_url or not headers_b64:
        return "Missing params", 400

    # --- HMAC Signature Verification ---
    if not sig or not hmac.compare_digest(sig, sign_url(target_url)):
        logging.warning(f"Forbidden: invalid or missing signature for {target_url}")
        return "Forbidden", 403

    try:
        headers = json.loads(base64.b64decode(headers_b64).decode("utf-8"))
    except:
        return "Invalid headers", 400

    keys_to_remove = [
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Accept-Encoding",
    ]
    for key in keys_to_remove:
        if key in headers:
            del headers[key]

    async with cffi_requests.AsyncSession(impersonate="chrome120") as session:
        try:
            resp = await session.get(target_url, headers=headers)

            if resp.status_code != 200:
                logging.error(f"Proxy failed: {resp.status_code} for {target_url}")
                return f"Proxy Error {resp.status_code}", resp.status_code

            content_type = resp.headers.get("Content-Type", "")

            if "mpegurl" in content_type or target_url.endswith(".m3u8"):
                content = resp.content
                text = content.decode("utf-8")
                new_lines = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        new_lines.append(line)
                    else:
                        absolute_link = urljoin(target_url, line)
                        encoded_headers = base64.b64encode(
                            json.dumps(headers).encode("utf-8")
                        ).decode("utf-8")
                        proxy_link = f"{url_for('proxy_stream', _external=True)}?url={quote(absolute_link)}&headers={encoded_headers}&sig={sign_url(absolute_link)}"
                        new_lines.append(proxy_link)

                final_resp = Response(
                    "\n".join(new_lines), mimetype="application/vnd.apple.mpegurl"
                )
                final_resp.headers["Cache-Control"] = (
                    "no-cache, no-store, must-revalidate"
                )
                final_resp.headers["Pragma"] = "no-cache"
                final_resp.headers["Expires"] = "0"
                return final_resp
            else:
                return Response(
                    resp.content, status=resp.status_code, mimetype=content_type
                )

        except Exception as e:
            logging.error(f"Proxy Exception: {e}")
            return str(e), 500


# --- Standard Routes ---


@app.route("/logo.png")
async def serve_logo():
    return await send_from_directory(".", "SportsSphereLogo.png")


@app.route("/manifest.json")
async def manifest():
    matches = await get_all_matches()
    available_sports = sorted(list(set(m.get("category", "Misc") for m in matches)))
    host_url = url_for("serve_logo", _external=True)

    return jsonify(
        {
            "id": "org.stremio.sportssphere",
            "version": "1.1.0",
            "name": "Sports Sphere",
            "description": "Aggregated Live Sports Events (Multi-Source)",
            "logo": host_url,
            "resources": ["catalog", "stream", "meta"],
            "types": ["movie"],
            "idPrefixes": ["pk_"],
            "catalogs": [
                {
                    "type": "movie",
                    "id": "pk_live",
                    "name": "Live Sports",
                    "extra": [{"name": "genre", "options": available_sports}],
                }
            ],
        }
    )


@app.route("/catalog/<type>/<id>.json")
@app.route("/catalog/<type>/<id>/genre=<genre>.json")
async def catalog(type, id, genre=None):
    if id != "pk_live":
        return jsonify({"metas": []})
    matches = await get_all_matches()
    metas = []
    now_ms = time.time() * 1000
    show_threshold = now_ms + 900000
    for m in matches:
        if genre and m.get("category") != genre:
            continue
        match_time = m.get("date", 0)
        if match_time > show_threshold:
            continue
        if match_time < (now_ms - 21600000):
            continue
        metas.append(
            {
                "id": f"pk_{m['id']}",
                "type": "movie",
                "name": m["title"],
                "poster": get_poster_url(m),
                "description": f"Live on Streamed.pk - {m.get('category', 'Sports')}",
                "genre": [m.get("category", "Misc")],
                "behaviorHints": {"defaultVideoId": f"pk_{m['id']}"},
            }
        )
    return jsonify({"metas": metas})


# --- WORKER ---
async def process_stream_option(embed_data, browser, semaphore):
    embed_url = embed_data["embed_url"]
    label = embed_data["label"]

    # If label starts with "Admin", change "Admin" text to "Alpha"
    if label.startswith("Admin"):
        label = label.replace("Admin", "Alpha")

    async with semaphore:
        data = await resolve_with_playwright(embed_url, browser)
    if not data or "url" not in data:
        return None

    stream_url = data["url"]
    headers = data["headers"]
    clean_root = data.get("clean_root", "")
    source = embed_data.get("source", "")

    # Golf streams need correct exposestrat.com Referer (captured by actual_referer fix above)
    if source == "golf":
        logging.info(
            f"Golf stream direct with Referer: {headers.get('Referer', 'N/A')}"
        )
        return {
            "title": f"{label} (Direct)",
            "url": stream_url,
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {"request": headers},
            },
        }

    is_strict = "strmd.top" in stream_url or "delta" in stream_url

    if is_strict:
        proxy_headers = headers.copy()
        proxy_headers["Referer"] = clean_root
        if "Origin" in proxy_headers:
            del proxy_headers["Origin"]

        headers_json = json.dumps(proxy_headers)
        headers_b64 = base64.b64encode(headers_json.encode("utf-8")).decode("utf-8")
        final_url = f"{url_for('proxy_stream', _external=True)}?url={quote(stream_url)}&headers={headers_b64}&sig={sign_url(stream_url)}"

        return {"title": f"{label} (Proxy)", "url": final_url}
    else:
        return {
            "title": f"{label} (Direct)",
            "url": stream_url,
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {"request": headers},
            },
        }


@app.route("/stream/<type>/<id>.json")
async def stream(type, id):
    if not id.startswith("pk_"):
        return jsonify({"streams": []})

    real_match_id = id.replace("pk_", "")
    current_time = time.time()

    if real_match_id in stream_cache:
        cached = stream_cache[real_match_id]
        if current_time < cached["expires_at"]:
            logging.info(f"CACHE HIT: {real_match_id}")
            return jsonify({"streams": cached["streams_list"]})
        else:
            del stream_cache[real_match_id]

    embeds_list = await get_all_stream_embeds(real_match_id)
    if not embeds_list:
        return jsonify({"streams": []})

    logging.info(f"Resolving {len(embeds_list)} options concurrently (max 2)...")

    # --- Shared Browser + Concurrency Limit ---
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--mute-audio",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        except Exception:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )

        semaphore = asyncio.Semaphore(2)
        tasks = [
            asyncio.create_task(process_stream_option(e, browser, semaphore))
            for e in embeds_list
        ]

        # --- Soft Timeout: return whatever resolved within the window ---
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=14)
        except asyncio.TimeoutError:
            logging.warning("Partial result returned due to timeout")
            # Cancel any still-pending tasks
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Collect results from tasks that already finished
            results = []
            for t in tasks:
                if t.done() and not t.cancelled():
                    try:
                        results.append(t.result())
                    except Exception:
                        results.append(None)

        await browser.close()

    valid_streams = [r for r in results if r]

    if not valid_streams:
        return jsonify({"streams": []})

    stream_cache[real_match_id] = {
        "streams_list": valid_streams,
        "expires_at": current_time + STREAM_CACHE_DURATION,
    }

    return jsonify({"streams": valid_streams})


@app.route("/meta/<type>/<id>.json")
async def meta(type, id):
    matches = await get_all_matches()
    real_id = id.replace("pk_", "")
    match = next((m for m in matches if m["id"] == real_id), None)
    if match:
        poster = get_poster_url(match)
        start_time = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(match.get("date") / 1000)
        )
        return jsonify(
            {
                "meta": {
                    "id": id,
                    "type": "movie",
                    "name": match["title"],
                    "poster": poster,
                    "background": poster,
                    "description": f"LIVE SPORT\nCategory: {match.get('category')}\nStart Time: {start_time}",
                }
            }
        )
    return jsonify({"meta": {}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
