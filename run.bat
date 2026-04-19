@echo off
cd /d "%~dp0"
python scraper.py >> logs\scraper.log 2>&1
