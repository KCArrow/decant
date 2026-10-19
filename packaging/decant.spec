# PyInstaller spec for the portable single-file build: dist/decant-x86_64
#
# Libraries that must match the user's own system are deliberately left out:
# graphics drivers (GL/EGL/DRM), X11/Wayland, fonts, glib, D-Bus and the C++
# runtime. Every Linux desktop has them, and bundling older copies from the
# build machine is the usual way portable Qt apps end up crashing on newer
# distros. Everything else Qt needs, including libxcb-cursor and xkbcommon
# (the ones often missing), is bundled.

import os
import re

HOST_LIBS = re.compile(
    r"^(libGL|libEGL|libGLX|libGLdispatch|libOpenGL|libdrm|libgbm"
    r"|libX11|libxcb\.so|libwayland-"
    r"|libfontconfig|libfreetype|libexpat"
    r"|libdbus-1|libglib-2|libgobject-2|libgio-2|libgmodule-2|libgthread-2"
    r"|libz\.so|libstdc\+\+|libgcc_s)"
)

a = Analysis(
    [os.path.join(SPECPATH, "..", "decant.py")],
    excludes=["tkinter"],
)
dropped = sorted({os.path.basename(b[0]) for b in a.binaries if HOST_LIBS.match(os.path.basename(b[0]))})
print("Leaving to the host system: " + ", ".join(dropped))
a.binaries = [b for b in a.binaries if not HOST_LIBS.match(os.path.basename(b[0]))]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="decant-x86_64",
    strip=False,
    upx=False,
    console=True,
)
