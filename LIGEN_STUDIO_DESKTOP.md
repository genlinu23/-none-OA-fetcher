# LigenStudio desktop entry

## Open

Use either entry:

- `C:\Users\logan\Desktop\LigenStudio.lnk`
- `C:\Users\logan\doi_harvest\dist\LigenStudio.exe`

The desktop shortcut uses `C:\Users\logan\doi_harvest` as the working directory so the exe can find the local `scripts` folder and write outputs under `C:\Users\logan\doi_harvest\outputs`.

## Rebuild

Run:

```cmd
C:\Users\logan\doi_harvest\scripts\build_ligen_studio_exe.cmd
```

If the command says PyInstaller is missing, install it into the bundled Python:

```cmd
C:\Users\logan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install pyinstaller
```

## Current limit

This is a packaged desktop app, not a full installer yet. It still relies on browser login, publisher access, and the local scripts in this project folder.
