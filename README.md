# ADA Checker

A Windows app that checks instructional files for accessibility against WCAG 2.1 AA, the standard required by the ADA Title II rule, plus WCAG 2.2 AA and PDF/UA.

Give it a file or a folder. It lists what to change in each file, where the problem is and how to fix it, and separately lists what a person needs to check by hand. It only reads files and never changes them. Everything runs on your own computer.

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

## Install

Requires Windows 10 or 11. Works on Intel, AMD and ARM based PCs.

1. Download this project (Code, Download ZIP) and unzip it to a folder that is not synced to OneDrive, for example `C:\Users\<you>\ADA-Checker`.
2. Double click `Setup.bat`. It installs Python if needed, installs the libraries, and adds shortcuts.

Run `Update.bat` about once a month to update the checking rules. See `READ ME FIRST.txt` for full instructions.

## Limits

Automated checks cannot confirm everything. A person still needs to judge whether alt text is accurate, whether math reads correctly in a PDF, whether the reading order makes sense, and whether video captions match what is said. Those items appear under Check manually. This tool does not certify compliance.

## Built with

[axe-core](https://github.com/dequelabs/axe-core) (MPL 2.0), [pikepdf](https://github.com/pikepdf/pikepdf), [PyMuPDF](https://github.com/pymupdf/PyMuPDF), [lxml](https://lxml.de), [openpyxl](https://openpyxl.readthedocs.io), [Pillow](https://python-pillow.org), [Playwright](https://playwright.dev/python/) and [requests](https://requests.readthedocs.io).
