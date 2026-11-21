# ADA Checker

A Windows app that checks instructional files for accessibility against WCAG 2.1 AA, the standard required by the ADA Title II rule, plus WCAG 2.2 AA and PDF/UA.

Give it a file or a folder. It lists what to change in each file, where the problem is and how to fix it, and separately lists what a person needs to check by hand. It only reads files and never changes them. Everything runs on your own computer, unless you turn on the optional AI review.

## What it checks

| File type | Highlights |
|---|---|
| Word (.docx) | Heading styles, alt text, table headers, blank lines used for spacing, typed lists, color contrast, equations, video placement, plus a checklist for exporting to PDF |
| PDF | Tags, title, language, headings, table headers, fonts, scanned pages, links, math that may not read correctly |
| Web pages (.html) | axe-core rules in Microsoft Edge in light and dark mode, with contrast warnings rechecked to remove false alarms |
| PowerPoint (.pptx) | Slide titles, alt text, reading order, table headers, contrast, links |
| Excel (.xlsx) | Sheet names, alt text, merged cells, tables, contrast, links |
| Brightspace course export (.zip) | Every supported file inside |

It also checks web links for broken pages and shows color blindness previews of pictures and graphs.

## Features

- Check one file, many files, or a whole folder with subfolders
- Right click any file or folder in File Explorer, Send to, ADA Checker
- Folder checks save a summary spreadsheet and one report per file
- Mark manual items as reviewed, or accept an item as is with a reason
- History tab with progress this month compared to last month
- Exportable compliance record (Excel) of every file, status, review and reason
- Option to show only what WCAG 2.1 AA requires
- Optional AI review of manual checks with Claude Code (see below)

## Install

Requires Windows 10 or 11. Works on Intel, AMD and ARM based PCs.

1. Download this project (Code, Download ZIP) and unzip it to a folder that is not synced to OneDrive, for example `C:\Users\<you>\ADA-Checker`.
2. Double click `Setup.bat`. It installs Python if needed, installs the libraries, and adds shortcuts.

Run `Update.bat` about once a month to update the checking rules. See `READ ME FIRST.txt` for full instructions.

## AI review (optional)

Turn on **Use AI review** in Settings to have Claude judge the Check manually items: alt text against the actual picture, math as a screen reader gets it, reading order, color use, contrast on backgrounds, and zoom and keyboard tests run in a real browser for web pages. It runs through [Claude Code](https://code.claude.com) on your computer, signed in with a Claude Pro or Max subscription, so no API key is needed. Any `ANTHROPIC_API_KEY` variable is removed when Claude Code is started, so it cannot bill an API account by accident.

For each file, the checker copies the evidence (text in reading order, each picture with its full alt text, PDF page images, browser screenshots and test results) to a temporary folder and starts Claude Code there with only its Read tool allowed. Claude returns pass, fix or unsure for every item. Pass items are marked "Reviewed by AI" and recorded that way in the compliance record. Fix and unsure items stay open with Claude's note and suggested fix. Video caption checks are never sent. When a file changes, its AI reviews are cleared.

Setup: in PowerShell run `irm https://claude.ai/install.ps1 | iex`, then run `claude` once and sign in.

## Limits

Automated checks cannot confirm everything. A person still needs to judge whether alt text is accurate, whether math reads correctly in a PDF, whether the reading order makes sense, and whether video captions match what is said. Those items appear under Check manually. AI review can settle many of them but can be wrong, so spot check it. This tool does not certify compliance.

## Built with

[axe-core](https://github.com/dequelabs/axe-core) (MPL 2.0), [pikepdf](https://github.com/pikepdf/pikepdf), [PyMuPDF](https://github.com/pymupdf/PyMuPDF), [lxml](https://lxml.de), [openpyxl](https://openpyxl.readthedocs.io), [Pillow](https://python-pillow.org), [Playwright](https://playwright.dev/python/) and [requests](https://requests.readthedocs.io).
