# PowerShell profile: PowerShell runs this file at every start.

# mdv: shows a Markdown file (or a directory's .md files) in the
# browser, styled like GitHub.  Example: mdv .\readme.md
# The viewer lives in C:\Users\bojan\Development\MdViewer.
function mdv {
  python C:\Users\bojan\Development\MdViewer\md_viewer.py @args
}
