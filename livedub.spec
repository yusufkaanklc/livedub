# PyInstaller build for Windows (.exe folder) and macOS (.app).
#   pip install -r requirements-build.txt
#   pyinstaller livedub.spec --noconfirm
# Output: dist/LiveDub/LiveDub.exe (Windows) or dist/LiveDub.app (macOS).
import sys

from PyInstaller.utils.hooks import collect_data_files

ICON = "livedub/assets/icon.png"  # converted to .ico/.icns by PyInstaller (needs Pillow)

datas = [(ICON, "livedub/assets")]
hiddenimports = []
if sys.platform == "win32":
    datas += collect_data_files("soundcard")  # cffi header files used at runtime
    hiddenimports += ["soundcard.mediafoundation"]

a = Analysis(
    ["launcher.py"],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LiveDub", console=False, icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, name="LiveDub")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="LiveDub.app",
        icon=ICON,
        bundle_identifier="com.livedub.app",
        info_plist={
            "NSMicrophoneUsageDescription": "LiveDub, çevirmek için mikrofonu veya seçtiğiniz ses girişini dinler.",
            "NSHighResolutionCapable": True,
        },
    )
