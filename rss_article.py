# -*- coding: utf-8 -*-
# Flask-based RSS to Wooostock uploader with enhanced failure logging and robust HTML parsing

import os
import re
import json
import hashlib
import logging
import requests
import feedparser
from pathlib import Path
from datetime import datetime, timedelta, timezone
from lxml import etree, html
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# Load environment variables
load_dotenv()

# ---------- Configuration ----------
API_URL     = os.getenv('API_URL')
API_KEY     = os.getenv('API_KEY')
RSS_URL     = os.getenv('RSS_URL')
POSTED_FILE = os.getenv('POSTED_FILE')

# Validate required environment variables
if not API_URL or not API_KEY or not RSS_URL or not POSTED_FILE:
    raise RuntimeError("Missing one of required env vars: API_URL, API_KEY, RSS_URL, POSTED_FILE")

POSTED_PATH = Path(POSTED_FILE)
FMT         = '%Y-%m-%d %H:%M:%S'
UA          = {'User-Agent': 'Mozilla/5.0 DecoTV-RSS/1.1'}

# ---------- Logging ----------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logger.addHandler(handler)

app = Flask(__name__)
md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
now = lambda: (datetime.now(timezone.utc) + timedelta(hours=8)).strftime(FMT)

# ---------- Utilities ----------
def load_posted() -> set[str]:
    if POSTED_PATH.exists():
        return {line.strip() for line in POSTED_PATH.read_text(encoding='utf-8').splitlines() if line.strip()}
    POSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSTED_PATH.write_text('', encoding='utf-8')
    return set()

def save_posted(uid: str):
    with POSTED_PATH.open('a', encoding='utf-8') as f:
        f.write(uid + '\n')

RX_STRIP_ATTRS = re.compile(r'\s+(?:srcset|sizes|decoding|fetchpriority|data-[^=]+|class|width|height|loading)="[^"]*"', flags=re.I)
SEP_RX = re.compile(r'[,，、]\s*')

def split_kw(s: str) -> list[str]:
    return [k.strip() for k in SEP_RX.split(s) if k.strip()]

def clean_img_html(img) -> str:
    t = etree.fromstring(etree.tostring(img))
    src, alt = t.get('src'), t.get('alt', '')
    t.attrib.clear(); t.set('src', src); t.set('style', 'width:100%;')
    if alt: t.set('alt', alt)
    return etree.tostring(t, encoding='unicode', method='html')

def strip_unwanted_attrs(html_str: str) -> str:
    return RX_STRIP_ATTRS.sub('', html_str)

def add_or_merge_style(tag: str, extra: str) -> str:
    if ' style=' not in tag:
        return tag.replace('>', f' style="{extra}">', 1)
    return re.sub(r'style="([^"]*)"', lambda m: f'style="{m.group(1).rstrip(";")}; {extra}"', tag, 1)

def fetch_pic(url: str):
    try:
        r = requests.get(url, headers=UA, timeout=10)
        r.raise_for_status()
        ext = r.headers.get('content-type', 'image/jpeg').split('/')[-1]
        return (f'image.{ext}', r.content, r.headers.get('content-type'))
    except Exception as e:
        logger.warning(f"Failed to fetch image {url}: {e}")
        return None

# ---------- Article Processing ----------
def push_article(url: str, seen: set[str]) -> str | None:
    uid = md5(url)
    if uid in seen:
        logger.info(f"Skipping duplicate: {url}")
        return None
    logger.info(f"Fetching: {url}")
    resp = requests.get(url, headers=UA, timeout=10)
    resp.raise_for_status()
    doc = html.fromstring(resp.text)
    doc.make_links_absolute(url)

    title = ''.join(doc.xpath('//h1[@class="page-title"]//text()')).strip()
    cover = ''.join(doc.xpath('//meta[@property="og:image"]/@content'))
    created = ''.join(doc.xpath('//meta[@property="article:published_time"]/@content'))
    created = created.replace('T',' ').split('+')[0] if created else now()

    kw = ['DecoTV'] + split_kw(''.join(doc.xpath('//meta[@name="keywords"]/@content'))) + doc.xpath('//div[contains(@class,"entry-tags")]//a/text()')
    seen_kw = set()
    keywords = ','.join([k for k in kw if not (k in seen_kw or seen_kw.add(k))])

    rest = ''.join(doc.xpath('//link[@rel="alternate" and @type="application/json"]/@href'))
    raw_json = requests.get(rest, headers=UA, timeout=10).text
    raw = json.loads(raw_json).get('content', {}).get('rendered', '')
    root = html.fromstring(raw)

    segs, first_img = [], ''
    for p in root.xpath('.//p'):
        if len(p) == 1 and p[0].tag == 'a' and len(p[0]) and p[0][0].tag == 'img':
            continue
        phs = []
        for i, img in enumerate(p.xpath('.//img')):
            ph = f'__IMG_{len(segs)}_{i}__'
            phs.append((ph, clean_img_html(img)))
            img.attrib.clear(); img.tag = 'span'; img.text = ph
            first_img = first_img or img.get('src')
        h = etree.tostring(p, encoding='unicode', method='html')
        h = add_or_merge_style(strip_unwanted_attrs(h), 'text-align: justify;').rstrip()
        for ph, ih in phs:
            h = h.replace(ph, ih)
        segs.append(h)
    for img in root.xpath('.//img[not(ancestor::p)]'):
        ih = clean_img_html(img)
        segs.append(f'<p style="text-align: justify;">{ih}</p>')
        first_img = first_img or img.get('src')

    body = '\n'.join(segs) or '<p style="text-align: justify;">（本文無內容）</p>'
    body += f"\n<p style=\"text-align: justify;\"><a href=\"{url}\">{title}</a></p>"

    cover = cover or first_img
    files = {'pic': fetch_pic(cover)} if cover else {}
    sign_time = now()
    payload = {
        'sign': md5(title + sign_time + API_KEY),
        'sign_time': sign_time,
        'title': title,
        'description': '',
        'content': body,
        'keywords': keywords,
        'type': '2', 'ac_type': '5', 'created_time': created
    }

    rsp = requests.post(API_URL, data=payload, files=files)
    if rsp.status_code == 409:
        logger.info("API duplicate")
        save_posted(uid)
        return None
    rsp.raise_for_status()
    save_posted(uid)
    logger.info(f"Pushed: {title}")
    return 'ok'

# ---------- RSS Processing with failure details ----------
def process_rss(feed_url: str):
    logger.info(f"RSS: {feed_url}")
    entries = feedparser.parse(feed_url).entries
    seen = load_posted()
    results = {'success': [], 'failed': []}
    for entry in entries:
        try:
            res = push_article(entry.link, seen)
            if res:
                results['success'].append(entry.link)
        except Exception as e:
            logger.error(f"Err {entry.link}: {e}")
            results['failed'].append({'url': entry.link, 'error': str(e)})
    return results, 200

# ---------- CLI scraper ----------
if __name__ == '__main__':
    from scrape import process_rss as rss_cli, RSS_URL as CLI_RSS_URL
    summary, _ = rss_cli(CLI_RSS_URL)
    print(summary)  # 用於 GitHub Actions

# ---------- Flask Routes ----------
@app.route('/', methods=['GET'])
 def root():
    url = request.args.get('url')
    if url:
        seen = load_posted()
        res = push_article(url, seen) or 'duplicate'
        return jsonify({'result': res}), 200
    data, code = process_rss(RSS_URL)
    return jsonify({'summary': data}), code

# ---------- Entry Point ----------
if __name__ == 'app':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
