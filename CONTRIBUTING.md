# Contributing

Thanks for your interest in improving this project.

## Development

```powershell
py -m pip install -r requirements.txt
py screenshot_masker.py
```

## Build

```powershell
py -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean ScreenshotMasker.spec
```

Before submitting changes, make sure the app can start on Windows and the default screenshot hotkey works.
