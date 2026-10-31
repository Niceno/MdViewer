# MdViewer

**MdViewer** shows Markdown files in the browser, styled like GitHub.  It
is a single **Python** script, `md_viewer.py`, with no dependencies: the
Markdown parser is part of it, so there is nothing to install.

Leave it running while you edit: each time the file is saved, the page
reloads itself and keeps its scroll position.

## Starting It

From a **PowerShell** terminal:

```powershell
python C:\Users\bojan\Development\MdViewer\md_viewer.py .\readme.md
```

The browser opens on the file, and the terminal keeps serving it until you
press `Ctrl+C`.  Use another terminal for editing.

> **_Note:_** Python reads the script only when it starts.  After a change
> to `md_viewer.py`, stop the viewer with `Ctrl+C` and start it again.

Other ways to use it:

| Command                               | What it does                         |
|---------------------------------------|--------------------------------------|
| `md_viewer.py FILE.md`                | shows the file in the browser        |
| `md_viewer.py DIRECTORY`              | lists the `.md` files of a directory |
| `md_viewer.py FILE.md -o FILE.html`   | writes a static HTML page instead    |
| `md_viewer.py FILE.md -n`             | serves without opening the browser   |
| `md_viewer.py -h`                     | lists all the options                |

In the browser, the key `t` switches between the light and the dark theme.

## What It Supports

- CommonMark: headings, paragraphs, emphasis, lists, quotes, code,
  links, images and HTML (comments stay hidden, as on GitHub);
- the GitHub extras: tables, strikethrough, task lists, links from bare
  URLs, alerts (`> [!NOTE]`) and the heading anchors GitHub generates, so
  tables of contents work;
- formulas in LaTeX, `$...$` inline and `$$...$$` displayed, drawn by
  **MathJax**.

> **_Note:_** MathJax comes from the internet, so formulas need a
> connection.  Pages without formulas don't load it.

Links between files work: the viewer serves the whole git repository
containing the file, so a click on `technical_reference.md` opens that
file, and images show up.  It only listens on the computer itself
(`127.0.0.1`) and refuses paths outside the repository.

## What It Doesn't Do

- It doesn't colour code blocks.
- It isn't a complete CommonMark implementation: rare corner cases may
  look different from GitHub.
- It has no emoji (`:smile:`), footnotes or Mermaid diagrams.
