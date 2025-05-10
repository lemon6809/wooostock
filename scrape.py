# scrape.py
import os
from dotenv import load_dotenv
from rss_article import process_rss, RSS_URL

# 载入 GitHub Actions 里设的环境变量
load_dotenv()

# 取环境变量或 fallback 到源码里的默认 RSS_URL
feed = os.getenv('RSS_URL', RSS_URL)

# 运行爬虫并打印 summary
result = process_rss(feed)
print(result)
