**decant.py** is the script. It needs Python 3.9+ and PySide6 (`pip install --user PySide6`, or `sudo pacman -S pyside6` on Arch).

**decant-x86_64** is the portable build, with Python and Qt inside. Make it executable and run it: `chmod +x decant-x86_64 && ./decant-x86_64`

**SHA256SUMS** has checksums for both: `sha256sum -c SHA256SUMS --ignore-missing`

Both need `bzcat` (bzip2), util-linux and polkit, which every desktop Linux has. Setup, usage and troubleshooting are in the [README](https://github.com/KCArrow/decant#readme).
